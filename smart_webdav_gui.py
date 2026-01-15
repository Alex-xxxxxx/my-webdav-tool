import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import requests
from flask import Flask, request, Response, stream_with_context
import logging
import json
import os
import sys
import pystray
from PIL import Image, ImageDraw
import socket

# ================= 全局配置与状态 =================

CONFIG_FILE = 'webdav_config.json'
DEFAULT_CONFIG = {
    "tailscale_url": "http://192.168.100.10:5000",
    "cloudflare_url": "https://nas.example.com",
    "local_port": 8888,
    "auto_start": False
}

# 用于在线程间共享状态
class AppState:
    def __init__(self):
        self.running = False
        self.config = DEFAULT_CONFIG.copy()
        self.server_thread = None
        self.flask_app = None

app_state = AppState()

# ================= 核心代理逻辑 (Flask) =================

flask_app = Flask(__name__)
# 关闭 Flask 默认的控制台日志，改用自定义处理
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

READ_METHODS = ['GET', 'HEAD', 'OPTIONS', 'PROPFIND']
HOP_BY_HOP_HEADERS = [
    'connection', 'keep-alive', 'proxy-authenticate', 
    'proxy-authorization', 'te', 'trailers', 
    'transfer-encoding', 'upgrade'
]

def log_message(message):
    """发送日志到 GUI"""
    if gui_instance:
        gui_instance.append_log(message)
    else:
        print(message)

def get_target_url(method, path):
    conf = app_state.config
    if method.upper() in READ_METHODS:
        base = conf.get("tailscale_url", "").rstrip('/')
        route_type = "READ (Tailscale)"
    else:
        base = conf.get("cloudflare_url", "").rstrip('/')
        route_type = "WRITE (Cloudflare)"
    
    target = f"{base}/{path}"
    if request.query_string:
        target = f"{target}?{request.query_string.decode('utf-8')}"
    return target, route_type

def clean_headers(headers):
    cleaned = {}
    for key, value in headers.items():
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != 'host':
            cleaned[key] = value
    return cleaned

@flask_app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PROPFIND', 'MKCOL', 'MOVE', 'COPY', 'LOCK', 'UNLOCK', 'PROPPATCH'])
@flask_app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PROPFIND', 'MKCOL', 'MOVE', 'COPY', 'LOCK', 'UNLOCK', 'PROPPATCH'])
def proxy(path):
    method = request.method
    target_url, route_type = get_target_url(method, path)
    
    incoming_headers = clean_headers(request.headers)
    if 'User-Agent' not in incoming_headers:
        incoming_headers['User-Agent'] = 'SmartWebDAVProxy/2.0'

    log_message(f"[{method}] {route_type}: /{path}")

    try:
        resp = requests.request(
            method=method,
            url=target_url,
            headers=incoming_headers,
            data=request.stream,
            cookies=request.cookies,
            allow_redirects=False,
            stream=True
        )

        excluded_headers = HOP_BY_HOP_HEADERS + ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [
            (name, value) for (name, value) in resp.headers.items()
            if name.lower() not in excluded_headers
        ]

        return Response(
            stream_with_context(resp.iter_content(chunk_size=4096)),
            status=resp.status_code,
            headers=headers
        )
    except Exception as e:
        err_msg = f"Error: {str(e)}"
        log_message(err_msg)
        return Response(err_msg, status=502)

def run_flask():
    """在独立线程中运行 Flask"""
    try:
        port = int(app_state.config.get("local_port", 8888))
        log_message(f"服务启动中... 监听端口: {port}")
        # threaded=True 对 Windows 挂载至关重要
        flask_app.run(host='127.0.0.1', port=port, threaded=True, use_reloader=False)
    except Exception as e:
        log_message(f"服务启动失败: {e}")

# ================= GUI 界面 (Tkinter) =================

class SmartGatewayGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("智能 WebDAV 网关")
        self.root.geometry("500x550")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        
        self.load_config()
        self.create_widgets()
        
        # 托盘图标相关
        self.tray_icon = None
        self.setup_tray()

        if self.config.get("auto_start", False):
            self.toggle_server()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    self.config = json.load(f)
            except:
                self.config = DEFAULT_CONFIG.copy()
        else:
            self.config = DEFAULT_CONFIG.copy()
        app_state.config = self.config

    def save_config(self):
        self.config["tailscale_url"] = self.ts_url_var.get()
        self.config["cloudflare_url"] = self.cf_url_var.get()
        self.config["local_port"] = self.port_var.get()
        self.config["auto_start"] = self.auto_start_var.get()
        
        app_state.config = self.config
        
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=4)
            messagebox.showinfo("成功", "配置已保存")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

    def create_widgets(self):
        # 样式
        style = ttk.Style()
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9))

        # 容器
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 标题
        ttk.Label(main_frame, text="智能双路 WebDAV 挂载", font=("Microsoft YaHei UI", 14, "bold")).pack(pady=(0, 20))

        # 配置表单区域
        form_frame = ttk.LabelFrame(main_frame, text="路由配置", padding="10")
        form_frame.pack(fill=tk.X, pady=5)

        # Tailscale 输入
        ttk.Label(form_frame, text="下载线路 (Tailscale URL):").pack(anchor=tk.W)
        self.ts_url_var = tk.StringVar(value=self.config.get("tailscale_url"))
        ttk.Entry(form_frame, textvariable=self.ts_url_var).pack(fill=tk.X, pady=(0, 10))

        # Cloudflare 输入
        ttk.Label(form_frame, text="上传线路 (Cloudflare URL):").pack(anchor=tk.W)
        self.cf_url_var = tk.StringVar(value=self.config.get("cloudflare_url"))
        ttk.Entry(form_frame, textvariable=self.cf_url_var).pack(fill=tk.X, pady=(0, 10))

        # 端口输入
        port_frame = ttk.Frame(form_frame)
        port_frame.pack(fill=tk.X)
        ttk.Label(port_frame, text="本地监听端口:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=self.config.get("local_port"))
        ttk.Entry(port_frame, textvariable=self.port_var, width=10).pack(side=tk.LEFT, padx=5)

        # 自动启动
        self.auto_start_var = tk.BooleanVar(value=self.config.get("auto_start", False))
        ttk.Checkbutton(form_frame, text="程序启动时自动开启服务", variable=self.auto_start_var).pack(side=tk.LEFT, padx=20)

        # 按钮区域
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=15)
        
        self.start_btn = ttk.Button(btn_frame, text="启动服务", command=self.toggle_server)
        self.start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        ttk.Button(btn_frame, text="保存配置", command=self.save_config).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # 日志区域
        log_frame = ttk.LabelFrame(main_frame, text="运行日志", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state='disabled', font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def append_log(self, msg):
        def _update():
            self.log_text.configure(state='normal')
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state='disabled')
        self.root.after(0, _update)

    def toggle_server(self):
        if app_state.running:
            # Flask 本身很难优雅停止，这里提示用户重启程序
            messagebox.showwarning("提示", "停止服务需要重启应用程序。\n请关闭程序后重新打开。")
            return

        # 保存当前配置到内存
        app_state.config["tailscale_url"] = self.ts_url_var.get()
        app_state.config["cloudflare_url"] = self.cf_url_var.get()
        app_state.config["local_port"] = self.port_var.get()

        # 启动线程
        t = threading.Thread(target=run_flask, daemon=True)
        t.start()
        app_state.running = True
        app_state.server_thread = t
        
        self.start_btn.config(text="服务运行中 (需重启以停止)", state='disabled')
        self.append_log(">>> 服务已启动")
        self.append_log(f">>> WebDAV 本地地址: http://127.0.0.1:{app_state.config['local_port']}")

    # ================= 托盘图标逻辑 =================

    def create_image(self):
        # 动态生成一个简单的图标 (蓝色方块)，避免依赖外部 .ico 文件
        w, h = 64, 64
        image = Image.new('RGB', (w, h), (30, 144, 255))
        dc = ImageDraw.Draw(image)
        dc.rectangle((16, 16, 48, 48), fill='white')
        return image

    def setup_tray(self):
        image = self.create_image()
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self.show_window, default=True),
            pystray.MenuItem("退出", self.quit_app)
        )
        self.tray_icon = pystray.Icon("SmartGateway", image, "Smart WebDAV Gateway", menu)
        
        # 在独立线程运行托盘，防止阻塞 GUI
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def hide_window(self):
        self.root.withdraw()
        # 气泡提示 (可选，某些系统可能不支持)
        # self.tray_icon.notify("程序已最小化到托盘", "Smart WebDAV")

    def show_window(self, icon, item):
        self.root.after(0, self.root.deiconify)

    def quit_app(self, icon, item):
        self.tray_icon.stop()
        self.root.quit()
        sys.exit()

# ================= 主程序入口 =================

if __name__ == '__main__':
    # 解决高分屏模糊问题
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass

    root = tk.Tk()
    gui_instance = SmartGatewayGUI(root)
    
    # 居中窗口
    ws = root.winfo_screenwidth()
    hs = root.winfo_screenheight()
    x = (ws/2) - (500/2)
    y = (hs/2) - (550/2)
    root.geometry('+%d+%d' % (x, y))
    
    root.mainloop()
