"""基于frp编写的远程控制脚本"""
from __future__ import annotations
from pathlib import Path
from collections import deque
import os
import sys,requests,time,subprocess,threading,hashlib,socket,struct,json,ctypes
import traceback
from io import BytesIO
from PIL import Image, ImageGrab
from pynput.keyboard import Controller as Keyboard
from pynput.keyboard import Key
from pynput.mouse import Controller as Mouse
from pynput.mouse import Button



"""被控端数据包类型"""
TYPE_SCREEN = 1
TYPE_MOUSE = 2
TYPE_KEYBOARD = 3
TYPE_COMMAND = 4
TYPE_COMMAND_RESULT = 5
TYPE_ERROR = 6
TYPE_STATS = 7
TYPE_SPEED_TEST = 8
TYPE_SPEED_DATA = 9

"""资源地址与重试间隔"""
FRPC_EXE_URL = "" # frpc.exe下载链接
FRPC_TOML_URL = "" # frpc.toml下载链接
RETRY_INTERVAL = 5
RETRY_REPORT_EVERY = 12

"""时间格式"""
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

"""压测期间暂停画面推流,否则画面会抢走一部分带宽"""
PAUSE_STREAM = threading.Event()


class GetAppPath:
    """
    用于获取程序所在目录和打包成exe后运行时绑定的目录
    """
    @staticmethod
    def get_app_dir() -> Path:
        """
        用于获取程序所在的目录
        :return:程序所在目录
        """
        if getattr(sys,"frozen",False):
            return Path(sys.executable).resolve().parent
        else:
            return Path(__file__).resolve().parent

    @staticmethod
    def get_bundle_dir() -> Path:
        """
        打包成exe后,用于获取exe内部目录
        :return:exe内部目录
        """
        if hasattr(sys,"_MEIPASS"):
            return Path(sys._MEIPASS)
        else:
            return GetAppPath.get_app_dir()

def describe_error(error:BaseException,with_position:bool = True) -> str:
    """
    把异常整理成一行便于阅读的错误信息
    :param error:异常对象
    :param with_position:是否带上出错位置
    :return:错误信息
    """
    message = f"{type(error).__name__}: {error}".strip().rstrip(":")
    if not with_position:
        return message
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return message
    position = frames[-1]
    return f"{message}（{Path(position.filename).name} 第{position.lineno}行）"

class ErrorReporter:
    """
    把被控端运行中出现的错误实时发送到控制端屏幕上
    """
    BACKLOG_SIZE = 30
    REPEAT_WINDOW = 5.0
    MAX_RECORDS = 200

    def __init__(self):
        self.socket_server:SocketServer | None = None
        self.lock = threading.Lock()
        self.backlog:deque[dict] = deque(maxlen = self.BACKLOG_SIZE)
        self.records:dict[tuple[str,str],tuple[float,int]] = {}

    def bind(self,socket_server:SocketServer) -> None:
        """
        绑定socket服务,之后出现的错误会立刻发送给控制端
        :param socket_server:socket服务
        :return:None
        """
        self.socket_server = socket_server

    def report(self,source:str,message:str,level:str = "错误") -> None:
        """
        上报一条错误信息,控制端没连接时先暂存,连接后补发
        :param source:出现错误的位置
        :param message:错误内容
        :param level:信息级别
        :return:None
        """
        try:
            repeats = self.count_repeat(source,message)
            if repeats is None:
                return
            if repeats:
                message = f"{message}（刚才重复出现{repeats}次）"
            self.send({
                "time": time.strftime(TIME_FORMAT),
                "level": level,
                "source": source,
                "message": message,
            })
        except Exception:
            pass

    def count_repeat(self,source:str,message:str) -> int | None:
        """
        同一个错误短时间内反复出现时只上报一次,避免刷屏
        :param source:出现错误的位置
        :param message:错误内容
        :return:需要上报时返回被压掉的次数,需要压掉时返回None
        """
        key = (source,message)
        now = time.monotonic()
        with self.lock:
            record = self.records.get(key)
            if record is not None and now - record[0] < self.REPEAT_WINDOW:
                self.records[key] = (record[0],record[1] + 1)
                return None
            if len(self.records) >= self.MAX_RECORDS:
                self.records.clear()
            self.records[key] = (now,0)
            return 0 if record is None else record[1]

    def send(self,item:dict) -> None:
        """
        发送一条错误信息,发送失败就暂存起来
        :param item:错误信息
        :return:None
        """
        server = self.socket_server
        if server is None:
            self.cache(item)
            return
        payload = json.dumps(item,ensure_ascii=False).encode("utf-8")
        try:
            server.send_packet(TYPE_ERROR,payload)
        except (ConnectionError,OSError,TypeError,ValueError):
            self.cache(item)

    def cache(self,item:dict) -> None:
        """
        暂存错误信息,等待控制端连接后补发
        :param item:错误信息
        :return:None
        """
        with self.lock:
            self.backlog.append(item)

    def flush_backlog(self) -> None:
        """
        控制端连接成功后,把连接前发生的错误补发过去
        :return:None
        """
        with self.lock:
            pending = list(self.backlog)
            self.backlog.clear()
        if not pending:
            return
        self.report(
            "错误上报",
            f"以下{len(pending)}条是控制端连接之前发生的错误",
            level = "提示",
        )
        for item in pending:
            self.send(item)

REPORTER = ErrorReporter()

class SpeedTester:
    """
    隧道带宽压测:控制端下发命令后,被控端一直往控制端灌不可压缩的数据,
    控制端按实际收到的字节数就能算出这条隧道现在能跑多少带宽
    """
    CHUNK_SIZE = 256 * 1024
    DEFAULT_SECONDS = 8.0
    MAX_SECONDS = 30.0
    MAX_CHUNK_SIZE = 4 * 1024 * 1024

    def __init__(self):
        self.lock = threading.Lock()
        self.thread:threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.direction = ""
        self.receive_lock = threading.Lock()
        self.received_bytes = 0
        self.receive_started = 0.0

    @property
    def running(self) -> bool:
        """压测是否正在进行"""
        thread = self.thread
        return thread is not None and thread.is_alive()

    def start(self,socket_server:SocketServer,seconds:float = 0.0,chunk_size:int = 0,direction:str = "up") -> None:
        """
        开始压测,压测期间暂停画面推流
        :param socket_server:socket服务
        :param seconds:压测时长
        :param chunk_size:每次发送的数据块大小
        :param direction:up表示被控端发数据,down表示控制端发数据
        :return:None
        """
        with self.lock:
            self.stop(socket_server)
            seconds = min(
                max(seconds or self.DEFAULT_SECONDS,1.0),
                self.MAX_SECONDS,
            )
            chunk_size = min(
                max(chunk_size or self.CHUNK_SIZE,4096),
                self.MAX_CHUNK_SIZE,
            )
            self.stop_flag.clear()
            PAUSE_STREAM.set()
            if direction == "down":
                # 反向压测:控制端往被控端灌数据,这里只负责收和统计
                with self.receive_lock:
                    self.direction = "down"
                    self.received_bytes = 0
                    self.receive_started = time.monotonic()
                self.thread = threading.Thread(
                    target = self.run_receive,
                    args = (socket_server,seconds),
                    name = "speed_test_rx",
                    daemon = True,
                )
            else:
                self.thread = threading.Thread(
                    target = self.run,
                    args = (socket_server,seconds,chunk_size),
                    name = "speed_test",
                    daemon = True,
                )
            self.thread.start()

    def stop(self,socket_server:SocketServer | None = None) -> None:
        """
        停止压测并恢复画面推流
        :param socket_server:socket服务,反向压测要用它把结果报回去
        :return:None
        """
        self.stop_flag.set()
        if self.direction == "down" and socket_server is not None:
            self.finish_receive(socket_server)
        thread = self.thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout = 3.0)
        self.thread = None
        PAUSE_STREAM.clear()

    def count_received(self,size:int) -> None:
        """
        反向压测期间累计收到的字节数
        :param size:本次收到的数据大小
        :return:None
        """
        with self.receive_lock:
            if self.direction != "down":
                return
            self.received_bytes += size

    def run_receive(self,socket_server:SocketServer,seconds:float) -> None:
        """
        反向压测:等控制端把数据发完(或者超时),再把收到的量报回去
        :param socket_server:socket服务
        :param seconds:压测时长
        :return:None
        """
        started = time.monotonic()
        try:
            self.send_message(socket_server,{
                "action": "started",
                "direction": "down",
                "time": time.strftime(TIME_FORMAT),
                "seconds": seconds,
            })
        except (ConnectionError,OSError):
            pass
        while not self.stop_flag.is_set():
            if time.monotonic() - started >= seconds + 5:
                break
            time.sleep(0.05)
        try:
            self.finish_receive(socket_server)
        except (ConnectionError,OSError):
            PAUSE_STREAM.clear()

    def finish_receive(self,socket_server:SocketServer) -> None:
        """
        反向压测结束:把收到的数据量和实际时长报给控制端
        :param socket_server:socket服务
        :return:None
        """
        with self.receive_lock:
            if self.direction != "down":
                return
            self.direction = ""
            received = self.received_bytes
            elapsed = max(time.monotonic() - self.receive_started,0.001)
        PAUSE_STREAM.clear()
        self.send_message(socket_server,{
            "action": "finished",
            "direction": "down",
            "time": time.strftime(TIME_FORMAT),
            "received_bytes": received,
            "seconds": round(elapsed,3),
            "bitrate": int(received * 8 / elapsed),
        })

    def run(self,socket_server:SocketServer,seconds:float,chunk_size:int) -> None:
        """
        持续发送数据,结束后把被控端这边的统计结果报给控制端
        :param socket_server:socket服务
        :param seconds:压测时长
        :param chunk_size:数据块大小
        :return:None
        """
        block = os.urandom(chunk_size)
        sent_bytes = 0
        started = time.monotonic()
        try:
            self.send_message(socket_server,{
                "action": "started",
                "direction": "up",
                "time": time.strftime(TIME_FORMAT),
                "seconds": seconds,
                "chunk_size": chunk_size,
            })
            while not self.stop_flag.is_set():
                if time.monotonic() - started >= seconds:
                    break
                socket_server.send_packet(TYPE_SPEED_DATA,block)
                sent_bytes += len(block) + socket_server.Head_Size
        except (ConnectionError,OSError):
            pass
        except Exception as error:
            REPORTER.report("带宽压测",f"压测发送失败：{describe_error(error)}")
        finally:
            elapsed = max(time.monotonic() - started,0.001)
            PAUSE_STREAM.clear()
            try:
                self.send_message(socket_server,{
                    "action": "finished",
                    "direction": "up",
                    "time": time.strftime(TIME_FORMAT),
                    "sent_bytes": sent_bytes,
                    "seconds": round(elapsed,3),
                    "bitrate": int(sent_bytes * 8 / elapsed),
                })
            except (ConnectionError,OSError):
                pass

    def send_message(self,socket_server:SocketServer,payload:dict) -> None:
        """
        把压测过程信息发给控制端
        :param socket_server:socket服务
        :param payload:信息内容
        :return:None
        """
        socket_server.send_packet(
            TYPE_SPEED_TEST,
            json.dumps(payload,ensure_ascii=False).encode("utf-8"),
        )

SPEED_TESTER = SpeedTester()

class Frpc:
    """
    用于查找frpc路径,启动frpc,重启frpc,下载frpc,守护frpc,确认frpc配置
    """
    def __init__(self):
        self.frpc_name = "frpc.exe"
        self.frpc_toml_name = "frpc_1.toml"
        self.stop_flag = False
        self.process:subprocess.Popen | None = None
        self.start_lock = threading.Lock()

    def find_frpc_path(self) -> Path:
        """
        用exe内部目录的frpc,exe内部没有就下载到exe内部目录
        :return:frpc路径
        """
        bundle_frpc_path = GetAppPath.get_bundle_dir() / f"{self.frpc_name}"
        if not (bundle_frpc_path.exists() and bundle_frpc_path.is_file()):
            self.download_frpc(bundle_frpc_path)
        return bundle_frpc_path

    def find_frpc_toml(self) -> Path:
        """
        用exe内部目录的配置(打包时塞进exe的那份),exe内部没有就下载到exe内部目录
        :return:配置文件路径
        """
        bundle_frpc_toml_path = GetAppPath.get_bundle_dir() / f"{self.frpc_toml_name}"
        if not (bundle_frpc_toml_path.exists() and bundle_frpc_toml_path.is_file()):
            self.download_frpc_toml(bundle_frpc_toml_path)
        return bundle_frpc_toml_path

    def kill_frpc(self) -> None:
        """
        用于杀死frpc进程,防止每次运行程序时frpc多次启动
        :return:None
        """
        subprocess.run(
            ["taskkill", "/F", "/IM", f"{self.frpc_name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def watch_frpc_dog(self) -> None:
        """
        用于监控frpc.exe的存活,挂掉了自动重启
        :return:None
        """
        while not self.stop_flag:
            time.sleep(5)
            try:
                if self.process is None or self.process.poll() is not None:
                    REPORTER.report(
                        "frpc守护",
                        "检测到frpc已退出,正在重新启动",
                        level = "提示",
                    )
                    self.start_frpc()
            except Exception as error:
                REPORTER.report(
                    "frpc守护",
                    f"重启frpc失败,稍后继续尝试：{describe_error(error)}",
                )

    def watch_toml_dog(self) -> None:
        """
        用于循环比对本地frpc配置与服务器frpc配置
        :return:None
        """
        while not self.stop_flag:
            time.sleep(30)
            try:
                if not self.contrast_frpc_toml():
                    self.start_frpc()
            except Exception as error:
                REPORTER.report(
                    "frpc配置",
                    f"检测配置失败,稍后继续尝试：{describe_error(error)}",
                )

    @staticmethod
    def get_server_frpc_toml_hash() -> str | None:
        """
        获取服务器上frpc配置哈希值
        :return:服务器frpc配置哈希值,获取失败返回None
        """
        try:
            server_frpc_toml = requests.get(FRPC_TOML_URL, timeout=30)
            server_frpc_toml.raise_for_status()
            # 用原始字节对比,避免换行符(CRLF/LF)被转换后哈希永远不一致
            return hashlib.sha256(server_frpc_toml.content).hexdigest()
        except Exception:
            return None

    @staticmethod
    def get_local_frpc_toml_hash(frpc_toml_path: Path) -> str | None:
        """
        获取本地frpc配置哈希值
        :param frpc_toml_path:本地frpc配置路径
        :return:本地frpc配置哈希值,读取失败返回None
        """
        try:
            return hashlib.sha256(frpc_toml_path.read_bytes()).hexdigest()
        except Exception:
            return None

    def contrast_frpc_toml(self) -> bool:
        """
        通过哈希来对比本地frpc配置与服务器上的frpc配置是否一致,不一致就重新下载
        :return:是否一致的结果
        """
        frpc_toml_path = self.find_frpc_toml()
        server_toml_hash = self.get_server_frpc_toml_hash()
        local_toml_hash = self.get_local_frpc_toml_hash(frpc_toml_path)

        if server_toml_hash is None:
            return True

        if server_toml_hash != local_toml_hash:
            self.download_frpc_toml(frpc_toml_path)
            return False
        return True

    @staticmethod
    def download_frpc(frpc_path:Path) -> None:
        """
        下载frpc到程序所在目录,下载失败会一直重试
        :return:None
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                temp_path = frpc_path.with_name(
                    f"{frpc_path.name}.{threading.get_ident()}.download"
                )
                response = requests.get(FRPC_EXE_URL, timeout=30)
                response.raise_for_status()
                temp_path.write_bytes(response.content)
                temp_path.replace(frpc_path)
                if attempts > 1:
                    REPORTER.report(
                        "frpc下载",
                        f"重试{attempts}次后下载成功：{frpc_path.name}",
                        level = "提示",
                    )
                return
            except Exception as error:
                if attempts == 1 or attempts % RETRY_REPORT_EVERY == 0:
                    REPORTER.report(
                        "frpc下载",
                        f"第{attempts}次下载frpc失败,"
                        f"{RETRY_INTERVAL}秒后重试：{describe_error(error, False)}",
                    )
                time.sleep(RETRY_INTERVAL)

    @staticmethod
    def download_frpc_toml(frpc_toml_path:Path) -> None:
        """
        下载frpc配置到指定路径,下载失败会一直重试
        :param frpc_toml_path:配置文件保存路径
        :return:None
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                frpc_toml_file = requests.get(FRPC_TOML_URL, timeout=30)
                frpc_toml_file.raise_for_status()
                temp_path = frpc_toml_path.with_name(
                    f"{frpc_toml_path.name}.{threading.get_ident()}.download"
                )
                temp_path.write_bytes(frpc_toml_file.content)
                temp_path.replace(frpc_toml_path)
                if attempts > 1:
                    REPORTER.report(
                        "frpc下载",
                        f"重试{attempts}次后下载成功：{frpc_toml_path.name}",
                        level = "提示",
                    )
                return
            except Exception as error:
                if attempts == 1 or attempts % RETRY_REPORT_EVERY == 0:
                    REPORTER.report(
                        "frpc下载",
                        f"第{attempts}次下载frpc配置失败,"
                        f"{RETRY_INTERVAL}秒后重试：{describe_error(error, False)}",
                    )
                time.sleep(RETRY_INTERVAL)

    def start_frpc(self) -> None:
        """
        启动frpc:先结束残留的frpc进程,再确认frpc与配置存在,最后启动
        :return:None
        """
        with self.start_lock:
            # 先杀死所有frpc进程
            self.kill_frpc()
            # 确认frpc.exe存在
            frpc_path = self.find_frpc_path()
            # 确认frpc.toml存在
            frpc_toml = self.find_frpc_toml()
            # 启动frpc
            self.process = subprocess.Popen(
                [str(frpc_path),"-c",str(frpc_toml)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            REPORTER.report("frpc守护","frpc已启动",level = "提示")

    def start(self) -> None:
        """
        启动frpc和frpc的守护线程
        :return:None
        """
        self.stop_flag = False
        try:
            self.start_frpc()
        except Exception as error:
            REPORTER.report(
                "frpc启动",
                f"启动frpc失败,交由守护线程继续尝试：{describe_error(error)}",
            )
        threading.Thread(target = self.watch_frpc_dog,daemon = True).start()
        threading.Thread(target = self.watch_toml_dog,daemon = True).start()

    def stop(self) -> None:
        """
        停止frpc的守护线程并结束frpc进程
        :return:None
        """
        self.stop_flag = True
        self.kill_frpc()

class DXGICapturer:
    """
    用DXGI桌面复制抓屏(GPU加速),N卡/A卡/Intel核显都能用,
    需要 Windows 8 以上且驱动支持;不可用时由GetScreen退回PIL抓屏
    """
    def __init__(self,output_index:int = 0,target_fps:int = 60):
        import dxcam
        self.camera = None
        last_error:Exception | None = None
        # 笔记本上主屏可能挂在核显上,这里把常见的设备/输出组合都试一遍
        for device_index in (0,1):
            for output_idx in (output_index,0,1):
                try:
                    camera = dxcam.create(
                        device_idx = device_index,
                        output_idx = output_idx,
                        output_color = "BGRA",
                    )
                except Exception as error:
                    last_error = error
                    continue
                if camera is not None:
                    self.camera = camera
                    break
            if self.camera is not None:
                break
        if self.camera is None:
            raise RuntimeError(f"没有可用的显示输出：{last_error}")
        self.target_fps = target_fps
        self.started = False

    def start(self) -> None:
        """
        启动抓屏(DXGI内部会用单独线程持续取帧)
        :return:None
        """
        # 不用 video_mode:画面没变化时不重复拷贝,省CPU
        self.camera.start(target_fps = self.target_fps)
        self.started = True

    def stop(self) -> None:
        """
        停止抓屏
        :return:None
        """
        if not self.started:
            return
        try:
            self.camera.stop()
        finally:
            self.started = False

    def grab(self) -> Image.Image | None:
        """
        取最新一帧画面,不阻塞等新帧(拿不到时返回None,由上层复用上一帧)
        :return:PIL图片,这一瞬间还没有新帧时返回None
        """
        frame = self.camera.grab()
        if frame is None:
            return None
        return Image.frombytes(
            "RGB",
            (frame.shape[1],frame.shape[0]),
            frame.data,
            "raw",
            "BGRX",
        )

class PillowCapturer:
    """
    原来的抓屏方式(GDI),任何Windows机器都能用,作为DXGI的兜底方案
    """
    def start(self) -> None:
        """
        不需要额外启动
        :return:None
        """
        pass

    def stop(self) -> None:
        """
        不需要额外停止
        :return:None
        """
        pass

    def grab(self) -> Image.Image:
        """
        抓一帧画面
        :return:PIL图片
        """
        return ImageGrab.grab()

def create_capturer() -> tuple[object,str]:
    """
    挑一个能用的抓屏方式:DXGI优先,不行就用PIL
    :return:(抓屏对象, 抓屏方式名称)
    """
    choice = os.environ.get("BEIKONG_CAPTURE","auto").strip().lower()
    if choice in ("pil","gdi","off"):
        return PillowCapturer(),"PIL"
    try:
        capturer = DXGICapturer()
    except Exception as error:
        if choice == "dxgi":
            REPORTER.report(
                "抓屏",
                f"DXGI抓屏不可用,已退回PIL抓屏：{describe_error(error,False)}",
                level = "提示",
            )
        return PillowCapturer(),"PIL"
    return capturer,"DXGI"

class GetScreen:
    """
    捕获屏幕并转换为JPEG,同时根据实际码率自适应画质
    """
    INITIAL_WIDTH = 1920
    MIN_WIDTH = 1280
    MAX_WIDTH = 1920
    MIN_QUALITY = 24
    MAX_QUALITY = 55
    # 画面码率预算(bit/s): 按隧道真实能力留出余量,24Mbps上行用18Mbps左右
    STREAM_TARGET_BITRATE = 18_000_000
    # 目标帧率: 只是节奏上限,抓屏编码跑不到时不会因此降画质,只会帧率低一点
    STREAM_TARGET_FPS = 30
    # 单帧超过"每帧预算"的这个倍数时才立刻降一档画质
    FRAME_SIZE_TOLERANCE = 1.5
    try:
        BILINEAR = Image.Resampling.BILINEAR
    except AttributeError:
        BILINEAR = Image.BILINEAR

    def __init__(self):
        self.width = self.INITIAL_WIDTH
        self.quality = 30
        self.frame_count = 0
        self.bitrate_estimate = 0.0
        self.send_time = 0.0
        self.capturer = None
        self.capturer_name = "PIL"
        self.last_image = None

    def open_capturer(self) -> None:
        """
        选择并启动抓屏方式(DXGI优先,PIL兜底)
        :return:None
        """
        self.close_capturer()
        capturer,capturer_name = create_capturer()
        try:
            capturer.start()
        except Exception as error:
            REPORTER.report(
                "抓屏",
                f"{capturer_name}抓屏启动失败,改用PIL抓屏："
                f"{describe_error(error,False)}",
                level = "提示",
            )
            capturer,capturer_name = PillowCapturer(),"PIL"
            capturer.start()
        self.capturer = capturer
        self.capturer_name = capturer_name
        self.last_image = None
        REPORTER.report("抓屏",f"当前抓屏方式：{capturer_name}",level = "提示")

    def close_capturer(self) -> None:
        """
        停止抓屏
        :return:None
        """
        capturer = self.capturer
        self.capturer = None
        if capturer is None:
            return
        try:
            capturer.stop()
        except Exception:
            pass

    def grab_image(self) -> Image.Image:
        """
        抓一帧画面,GPU抓屏出问题时自动退回PIL抓屏
        :return:PIL图片
        """
        capturer = self.capturer
        if capturer is not None:
            try:
                image = capturer.grab()
            except Exception as error:
                REPORTER.report(
                    "抓屏",
                    f"GPU抓屏出错,改用PIL抓屏：{describe_error(error,False)}",
                )
                self.close_capturer()
                image = None
            if image is not None:
                self.last_image = image
                return image
            if self.last_image is not None:
                # 这一瞬间还没出新帧,先复用上一帧,别让画面卡住
                return self.last_image
        return ImageGrab.grab()

    def resize_image(self,image:Image.Image,target_width:int,target_height:int) -> Image.Image:
        """
        缩小画面:能整数倍缩小时用reduce(比双线性快好几倍),否则用双线性
        :param image:原图
        :param target_width:目标宽度
        :param target_height:目标高度
        :return:缩放后的图片
        """
        if image.size == (target_width,target_height):
            return image
        factor = image.width // target_width
        if factor >= 2:
            reduced = image.reduce(factor)
            if reduced.size == (target_width,target_height):
                return reduced
            return reduced.resize((target_width,target_height),self.BILINEAR)
        return image.resize((target_width,target_height),self.BILINEAR)

    def capture(self,interval:float) -> bytes:
        """
        抓取屏幕截图并转换为JPEG数据
        :param interval:截图间隔
        :return:JPEG数据
        """
        source = self.grab_image()
        source_width, source_height = source.size
        target_width = min(self.width,source_width)
        target_height = max(1,round(source_height * target_width / source_width))
        image = self.resize_image(source,target_width,target_height)
        data = self.encode_jpeg(image)
        target_frame_size = (self.STREAM_TARGET_BITRATE / 8 / self.STREAM_TARGET_FPS)
        retries = 0
        while len(data) > target_frame_size * self.FRAME_SIZE_TOLERANCE and retries < 2:
            retries += 1
            if self.quality > self.MIN_QUALITY:
                self.quality = max(self.MIN_QUALITY, self.quality - 6)
                data = self.encode_jpeg(image)
            elif self.width > self.MIN_WIDTH:
                self.width = max(self.MIN_WIDTH, int(self.width * 0.9))
                target_height = max(1,round(source_height * self.width / source_width))
                # 每次都从原图缩,避免在缩小过的图上再缩(又慢又糊)
                image = self.resize_image(source,self.width,target_height)
                data = self.encode_jpeg(image)
            else:
                break
        self.update_bitrate(len(data), interval)
        return data

    def encode_jpeg(self, image: Image.Image) -> bytes:
        """
        用于将图片转换为bytes数据
        :param image:屏幕截图
        :return:JPEG的bytes
        """
        buffer = BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=self.quality,
            subsampling=0,
            optimize=False,
            progressive=False,
        )
        return buffer.getvalue()

    def update_bitrate(self, frame_size: int, interval: float) -> None:
        """
        按实际码率调整画质:码率超预算或发送被堵住就降,余量大就升
        :param frame_size:屏幕大小
        :param interval:截图间隔
        :return:None
        """
        current_bitrate = frame_size * 8 / max(interval, 0.001)
        if self.bitrate_estimate:
            self.bitrate_estimate = (self.bitrate_estimate * 0.75+ current_bitrate * 0.25)
        else:
            self.bitrate_estimate = current_bitrate
        self.frame_count += 1
        if self.frame_count % 4:
            return
        # 网络是否被堵住:看发送耗时(发送被隧道卡住时会明显变长)
        congested = (
            self.send_time > max(interval * 0.25, 0.02)
            or self.bitrate_estimate > self.STREAM_TARGET_BITRATE * 0.9
        )
        if congested:
            if self.quality > self.MIN_QUALITY:
                self.quality = max(self.MIN_QUALITY, self.quality - 4)
            elif self.width > self.MIN_WIDTH:
                self.width = max(self.MIN_WIDTH, int(self.width * 0.94))
            return
        # 还有余量就往上加画质,画质到顶再加分辨率
        if self.bitrate_estimate < self.STREAM_TARGET_BITRATE * 0.8:
            if self.quality < self.MAX_QUALITY:
                self.quality = min(self.MAX_QUALITY, self.quality + 3)
            elif self.width < self.MAX_WIDTH:
                self.width = min(self.MAX_WIDTH, int(self.width * 1.04))

    def note_send_time(self, send_time: float) -> None:
        """
        记录上一帧发送耗时,发送被隧道堵住时会变长,用来判断网络是否拥塞
        :param send_time:发送耗时
        :return:None
        """
        self.send_time = send_time

class ScreenStreamer:
    """
    在独立线程中截屏和发送图片数据,避免阻塞鼠标与键盘控制
    """
    STATS_INTERVAL = 1.0
    TIMER_RESOLUTION = 1
    def __init__(self,socket_server,screen):
        self.socket_server = socket_server
        self.screen = screen
        self.stop_event = threading.Event()
        self.stats_started = 0.0
        self.stats_frames = 0
        self.stats_bytes = 0
        self.thread = threading.Thread(
            target = self.run,
            name = "screen_stream",
            daemon = True
        )

    def start(self) -> None:
        """
        启动线程函数
        :return:None
        """
        self.thread.start()

    def stop(self) -> None:
        """
        停止线程函数
        :return:None
        """
        self.stop_event.set()
        self.thread.join(timeout = 1.0)

    def run(self) -> None:
        """
        核心循环,确保获取屏幕截图和发送
        :return:None
        """
        frame_interval = 1 / self.screen.STREAM_TARGET_FPS
        last_frame_started = 0.0
        self.stats_started = time.monotonic()
        self.screen.open_capturer()
        self.raise_timer_resolution()
        try:
            while not self.stop_event.is_set():
                if PAUSE_STREAM.is_set():
                    # 压测期间先不推画面,把带宽让给压测
                    self.stop_event.wait(0.05)
                    continue
                frame_started = time.monotonic()
                interval = (
                    max(frame_started - last_frame_started, frame_interval)
                    if last_frame_started
                    else frame_interval
                )
                try:
                    frame = self.screen.capture(interval)
                    send_started = time.monotonic()
                    self.socket_server.send_packet(TYPE_SCREEN,frame)
                    self.screen.note_send_time(time.monotonic() - send_started)
                except (ConnectionError,OSError):
                    break
                except Exception as error:
                    REPORTER.report(
                        "画面推流",
                        f"抓屏或发送画面失败：{describe_error(error)}",
                    )
                    self.stop_event.wait(0.05)
                    continue
                self.count_frame(len(frame))
                last_frame_started = frame_started
                elapsed = time.monotonic() - frame_started
                wait_time = frame_interval - elapsed
                if wait_time > 0:
                    self.stop_event.wait(wait_time)
        finally:
            self.raise_timer_resolution(False)
            self.screen.close_capturer()

    @staticmethod
    def raise_timer_resolution(enable:bool = True) -> None:
        """
        提高系统定时器精度:Windows默认最小睡眠约15.6毫秒,会把帧节奏拖慢
        :param enable:True提高精度,False恢复
        :return:None
        """
        try:
            if enable:
                ctypes.windll.winmm.timeBeginPeriod(ScreenStreamer.TIMER_RESOLUTION)
            else:
                ctypes.windll.winmm.timeEndPeriod(ScreenStreamer.TIMER_RESOLUTION)
        except Exception:
            pass

    def count_frame(self, frame_size:int) -> None:
        """
        累计已发送的帧数和字节数,每秒把实际带宽和JPEG大小发给控制端
        :param frame_size:这一帧JPEG的字节数
        :return:None
        """
        now = time.monotonic()
        if now - self.stats_started >= self.STATS_INTERVAL * 2:
            # 推流停顿过(例如刚做完压测),重新开始统计,避免算出一个假的低带宽
            self.stats_started = now
            self.stats_frames = 0
            self.stats_bytes = 0
        self.stats_frames += 1
        self.stats_bytes += frame_size + self.socket_server.Head_Size
        elapsed = now - self.stats_started
        if elapsed < self.STATS_INTERVAL:
            return
        self.send_stats(elapsed,frame_size)
        self.stats_started = now
        self.stats_frames = 0
        self.stats_bytes = 0

    def send_stats(self,elapsed:float,frame_size:int) -> None:
        """
        把被控端这边的实际发送带宽、帧率、JPEG大小和画质发给控制端
        :param elapsed:统计时长
        :param frame_size:最后一帧JPEG的字节数
        :return:None
        """
        frames = self.stats_frames
        stats = {
            "time": time.strftime(TIME_FORMAT),
            "fps": round(frames / elapsed,1),
            "bitrate": int(self.stats_bytes * 8 / elapsed),
            "byte_rate": int(self.stats_bytes / elapsed),
            "frame_size": frame_size,
            "average_frame_size": int(self.stats_bytes / frames) if frames else 0,
            "quality": self.screen.quality,
            "width": self.screen.width,
            "target_fps": self.screen.STREAM_TARGET_FPS,
            "target_bitrate": self.screen.STREAM_TARGET_BITRATE,
        }
        try:
            self.socket_server.send_packet(
                TYPE_STATS,
                json.dumps(stats,ensure_ascii=False).encode("utf-8"),
            )
        except (ConnectionError,OSError):
            # 控制端已经断开,这一帧的统计丢掉即可
            pass

class ExecuteCmd:
    """
    用于执行cmd指令并返回结果
    """
    @staticmethod
    def run_cmd(command:str) -> str:
        """
        执行cmd指令
        :param command:cmd指令
        :return:cmd指令结果
        """
        try:
            process = subprocess.run(
                ["cmd.exe", "/c", command],
                capture_output=True,
                text=True,
                encoding="gbk",
                errors="replace",
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            output = ""
            if process.stdout:
                output += process.stdout
            if process.stderr:
                output += process.stderr
            output = output.strip()
            if output:
                return output
            if process.returncode == 0:
                return "命令执行成功，无返回内容"
            return f"命令执行失败，返回码：{process.returncode}"
        except subprocess.TimeoutExpired:
            return "命令执行超时"
        except Exception as e:
            return f"命令执行异常：{type(e).__name__}: {e}"

class KeyboardController:
    """
    用于控制端操作被控端的键盘
    """
    KEY_MAP = {
        "enter": Key.enter,
        "space": Key.space,
        "backspace": Key.backspace,
        "tab": Key.tab,
        "esc": Key.esc,
        "shift": Key.shift,
        "ctrl": Key.ctrl,
        "alt": Key.alt,
        "up": Key.up,
        "down": Key.down,
        "left": Key.left,
        "right": Key.right,
        "home": Key.home,
        "end": Key.end,
        "page_up": Key.page_up,
        "page_down": Key.page_down,
        "delete": Key.delete,
        "insert": Key.insert,
        "f1": Key.f1,
        "f2": Key.f2,
        "f3": Key.f3,
        "f4": Key.f4,
        "f5": Key.f5,
        "f6": Key.f6,
        "f7": Key.f7,
        "f8": Key.f8,
        "f9": Key.f9,
        "f10": Key.f10,
        "f11": Key.f11,
        "f12": Key.f12,
        "caps_lock": Key.caps_lock,
        "num_lock": Key.num_lock,
        "scroll_lock": Key.scroll_lock,
        "print_screen": Key.print_screen,
        "pause": Key.pause,
        "cmd": Key.cmd,
        "menu": Key.menu,
    }
    def __init__(self):
        self.keyboard = Keyboard()

    @classmethod
    def convert_key(cls, key) -> Key | str:
        """
        将字符串转换为pynput的按键操作
        :param key:按下的按键
        :return:按键操作
        """
        key = key.lower()
        if key in cls.KEY_MAP:
            return cls.KEY_MAP[key]
        if len(key) == 1:
            return key
        raise ValueError(f"未知键盘按键：{key}")

    def press(self, key) -> None:
        """
        按下某个键但不松开操作
        :param key:按下的按键
        :return:None
        """
        self.keyboard.press(self.convert_key(key))

    def release(self, key) -> None:
        """
        松开按键操作
        :param key:松开的按键
        :return:None
        """
        self.keyboard.release(self.convert_key(key))

    def tap(self, key) -> None:
        """
        按一下按键的操作
        :param key:按下的按键
        :return:None
        """
        self.press(key)
        self.release(key)

class MouseController:
    """
    用于控制端操作被控端的鼠标
    """
    BUTTON_MAP = {
        "left": Button.left,
        "right": Button.right,
        "middle": Button.middle,
    }

    def __init__(self):
        self.mouse = Mouse()

    @classmethod
    def convert_button(cls, button) -> Button:
        """
        将字符串转为鼠标按键操作
        :param button:鼠标按键
        :return:
        """
        button = button.lower()
        if button not in cls.BUTTON_MAP:
            raise ValueError(f"未知鼠标按键：{button}")
        return cls.BUTTON_MAP[button]

    def move(self, x, y) -> None:
        """
        移动鼠标
        :param x:x轴坐标
        :param y:y轴坐标
        :return:None
        """
        self.mouse.position = (x, y)

    def move_ratio(self, x_ratio, y_ratio) -> None:
        """
        按照屏幕比例移动鼠标
        :param x_ratio:x轴比例
        :param y_ratio:y轴比例
        :return:None
        """
        x_ratio = min(max(float(x_ratio), 0.0), 1.0)
        y_ratio = min(max(float(y_ratio), 0.0), 1.0)
        try:
            width = ctypes.windll.user32.GetSystemMetrics(0)
            height = ctypes.windll.user32.GetSystemMetrics(1)
        except Exception:
            width, height = 1280, 720
        self.move(round((width - 1) * x_ratio), round((height - 1) * y_ratio))

    def click(self, button="left", count=1) -> None:
        """
        鼠标点击操作
        :param button:鼠标按键
        :param count:点击次数
        :return:None
        """
        button = self.convert_button(button)
        self.mouse.click(button, count)

    def press(self, button="left") -> None:
        """
        鼠标按下操作
        :param button:鼠标按键
        :return:None
        """
        button = self.convert_button(button)
        self.mouse.press(button)

    def release(self, button="left") -> None:
        """
        鼠标松开操作
        :param button:鼠标按键
        :return:None
        """
        button = self.convert_button(button)
        self.mouse.release(button)

    def scroll(self, dx, dy) -> None:
        """
        鼠标滚轮操作
        :param dx:滚轮x轴
        :param dy:滚轮y轴
        :return:None
        """
        self.mouse.scroll(dx, dy)

class SocketServer:
    """
    被控端 TCP Socket 服务
    """
    Head_Size = 5
    def __init__(self):
        self.ip = "0.0.0.0"
        self.port = 9000
        self.server_socket = None
        self.client_socket = None
        self.client_address = None
        self.send_lock = threading.Lock()

    def start_socket(self) -> None:
        """
        创建并绑定服务端socket,开始监听控制端连接
        :return:None
        """
        server_socket = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        server_socket.bind((self.ip,self.port))
        server_socket.listen(1)
        self.server_socket = server_socket

    def accept_client(self) -> None:
        """
        阻塞等待控制端连接,连接成功后降低网络延迟
        :return:None
        """
        self.client_socket,self.client_address = self.server_socket.accept()
        self.configure_client()

    def recv_exact(self,size) -> bytes:
        """
        准确接收指定长度的数据
        :param size:数据长度
        :return:数据
        """
        data = bytearray()
        while len(data) < size:
            chunk = self.client_socket.recv(size - len(data))
            if not chunk:
                raise ConnectionError("控制端已断开连接")
            data.extend(chunk)
        return bytes(data)

    def recv_packet(self) -> tuple[int, bytes]:
        """
        接收一个完整的数据包:先读包头,再按包头中的长度读取包体
        :return:(数据包类型,数据包内容)
        """
        header = self.recv_exact(self.Head_Size)
        package_type,data_size = struct.unpack("!BI",header)
        data = self.recv_exact(data_size) if data_size else b""
        return package_type,data

    def send_packet(self,packet_type,data) -> None:
        """
        向控制端发送数据包
        :param packet_type:数据包类型
        :param data:数据
        :return:None
        """
        if not isinstance(data,bytes):
            raise TypeError(f"数据包内容必须是bytes，当前是{type(data).__name__}")
        header = struct.pack("!BI",packet_type,len(data))
        with self.send_lock:
            if self.client_socket is None:
                raise ConnectionError("控制端尚未连接")
            self.client_socket.sendall(header + data)

    def configure_client(self) -> None:
        """
        配置客户端连接以降低延迟
        :return:None
        """
        if self.client_socket is None:
            return
        self.client_socket.setsockopt(
            socket.IPPROTO_TCP,
            socket.TCP_NODELAY,
            1
        )
        self.client_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_KEEPALIVE,
            1,
        )

    def disconnect_client(self) -> None:
        """
        控制端断开连接后,保持监听
        :return:None
        """
        with self.send_lock:
            if self.client_socket is not None:
                try:
                    self.client_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.client_socket.close()
                except OSError:
                    pass
                self.client_socket = None
            self.client_address = None

    def close(self) -> None:
        """
        关闭当前连接和服务端
        :return:None
        """
        self.disconnect_client()
        with  self.send_lock:
            if self.server_socket:
                self.server_socket.close()
                self.server_socket = None

def run_command_async(command:str, socket_server:SocketServer) -> None:
    """
    在线程中执行cmd指令,避免阻塞
    :param command:cmd指令
    :param socket_server:socket服务
    :return:None
    """
    result = ExecuteCmd.run_cmd(command)
    response = result.encode("utf-8", errors="replace")
    try:
        socket_server.send_packet(TYPE_COMMAND_RESULT, response)
    except (ConnectionError, OSError):
        # 控制端已经断开,结果没人接收,不用当作错误上报
        pass

def handle_packet(
    packet_type: int,
    data: bytes,
    socket_server: SocketServer,
    mouse: MouseController,
    keyboard: KeyboardController,
    ) -> None:
    """
    处理控制端发送的数据包
    :param packet_type:数据包类型
    :param data:数据
    :param socket_server:socket服务
    :param mouse:鼠标操作
    :param keyboard:键盘操作
    :return:None
    """
    if packet_type == TYPE_SCREEN:
        return
    if packet_type == TYPE_COMMAND:
        command = data.decode("utf-8", errors="replace").strip()
        if command:
            threading.Thread(
                target=run_command_async,
                args=(command, socket_server),
                name="command-thread",
                daemon=True,
            ).start()
        return
    if packet_type == TYPE_SPEED_DATA:
        # 反向压测时控制端灌过来的数据,按大小累计(含包头)
        SPEED_TESTER.count_received(len(data) + socket_server.Head_Size)
        return
    if packet_type == TYPE_SPEED_TEST:
        payload = json.loads(data.decode("utf-8"))
        action = payload.get("action")
        if action == "start":
            SPEED_TESTER.start(
                socket_server,
                float(payload.get("seconds") or 0),
                int(payload.get("chunk_size") or 0),
                str(payload.get("direction") or "up"),
            )
        elif action == "stop":
            SPEED_TESTER.stop(socket_server)
        return
    payload = json.loads(data.decode("utf-8"))
    if packet_type == TYPE_MOUSE:
        action = payload.get("action")
        if action == "move":
            mouse.move_ratio(payload.get("x", 0), payload.get("y", 0))
        elif action == "click":
            mouse.click(
                payload.get("button", "left"),
                int(payload.get("count", 1)),
            )
        elif action == "press":
            mouse.press(payload.get("button", "left"))
        elif action == "release":
            mouse.release(payload.get("button", "left"))
        elif action == "scroll":
            mouse.scroll(
                int(payload.get("dx", 0)),
                int(payload.get("dy", 0)),
            )
        return
    if packet_type == TYPE_KEYBOARD:
        action = payload.get("action")
        key = str(payload.get("key", ""))
        if not key:
            return
        if action == "press":
            keyboard.press(key)
        elif action == "release":
            keyboard.release(key)
        elif action == "tap":
            keyboard.tap(key)

def main() -> None:
    """
    被控端启动函数
    :return:None
    """
    socket_server = SocketServer()
    REPORTER.bind(socket_server)
    frpc = None
    screen_streamer = None
    try:
        # 初始化模块
        screen = GetScreen()
        keyboard = KeyboardController()
        mouse = MouseController()
        # 创建Socket并开始监听控制端连接
        socket_server.start_socket()
        # 启动 frpc
        frpc = Frpc()
        frpc_thread = threading.Thread(
            target=frpc.start,
            name="frpc-thread",
            daemon=True
        )
        frpc_thread.start()
        # 控制端断开后继续监听,等待下一个控制端连接
        while True:
            socket_server.accept_client()
            REPORTER.report("连接","控制端已连接",level = "提示")
            # 把控制端连接之前发生的错误补发过去
            REPORTER.flush_backlog()
            screen_streamer = ScreenStreamer(socket_server, screen)
            screen_streamer.start()
            try:
                while True:
                    packet_type, data = socket_server.recv_packet()
                    try:
                        handle_packet(
                            packet_type,
                            data,
                            socket_server,
                            mouse,
                            keyboard,
                        )
                    except Exception as error:
                        REPORTER.report(
                            f"数据包处理(类型{packet_type})",
                            describe_error(error),
                        )
            except (ConnectionError, OSError):
                pass
            except Exception as error:
                # 单个连接出问题不能让被控端整个退出
                REPORTER.report("连接处理", f"连接异常：{describe_error(error)}")
            finally:
                screen_streamer.stop()
                screen_streamer = None
                socket_server.disconnect_client()
                REPORTER.report("连接","控制端已断开,继续等待连接",level = "提示")
    except KeyboardInterrupt:
        pass
    except Exception as error:
        REPORTER.report("被控端", f"异常退出：{describe_error(error)}")
    finally:
        if screen_streamer is not None:
            screen_streamer.stop()
        socket_server.close()
        if frpc is not None:
            try:
                frpc.stop()
            except Exception as error:
                REPORTER.report("frpc", f"停止frpc失败：{describe_error(error)}")

if __name__ == "__main__":
    main()
