#!/usr/bin/env python3
"""Icon in the macOS menu bar or in the Windows system tray with a menu.

Tk 8.6 has no such icon, so it is built with ctypes: NSStatusItem (macOS) and Shell_NotifyIcon
(Windows). A click on a menu entry never calls Tk directly (that is not safe from a native
callback): it is put in a queue which the Tk event loop reads.

Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import collections
import ctypes
import sys
import threading

AVAILABLE = sys.platform in ('darwin', 'win32')
POLL_MILLISECONDS = 100


class TrayIcon(object):
    """Icon with a menu. items() returns the entries [(label, command), None (separator), …]; it is asked
    regularly, so the menu follows the plugins and the language. The Tk event loop of root runs the commands."""

    def __init__(self, root, items, icon_png, icon_ico, tooltip='CANtrix'):
        self.root, self.items = root, items
        self.picked = collections.deque()  # indices of clicked entries (filled by the native callback)
        self.labels = None
        self.commands = []
        backend = _MacStatusItem if sys.platform == 'darwin' else _WindowsTrayIcon
        self.native = backend(self._pick, icon_png if sys.platform == 'darwin' else icon_ico, tooltip)
        self._job = None
        self.update()
        self._poll()

    def _pick(self, index):
        if 0 <= index < len(self.commands):
            self.picked.append(self.commands[index])

    def update(self):
        """Rebuild the menu if the entries changed."""
        entries = self.items()
        self.commands = [entry[1] if entry else None for entry in entries]
        labels = [entry[0] if entry else None for entry in entries]
        if labels != self.labels:
            self.labels = labels
            self.native.set_menu(labels)

    def _poll(self):
        self._job = self.root.after(POLL_MILLISECONDS, self._poll)  # first: a command may open a modal dialog
        self.update()
        self.native.show_in_dock(has_visible_window(self.root))
        while self.picked:
            command = self.picked.popleft()
            self.native.activate()
            self.root.after_idle(command)

    def destroy(self):
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
        self.picked.clear()
        self.native.destroy()


def windows_of(widget):
    """The Tk main window and all Toplevel windows below widget (without tooltips and the like)."""
    found = []
    if widget.winfo_class() in ('Tk', 'Toplevel') or widget is widget.winfo_toplevel():
        try:
            if not widget.wm_overrideredirect():
                found.append(widget)
        except Exception:  # not a window
            pass
    for child in widget.winfo_children():
        found.extend(windows_of(child))
    return found


def has_visible_window(root):
    return any(window.wm_state() != 'withdrawn' for window in windows_of(root))


# ---------------------------------------------------------------- macOS

_mac_target_class = None  # Objective-C class that receives the menu clicks (registered once per process)
_mac_pick = None  # callback(index) of the current status item
_mac_imp = None  # keeps the ctypes callback alive


class _CGSize(ctypes.Structure):
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]


class _MacStatusItem(object):
    """NSStatusItem in the menu bar. Tk runs the Cocoa event loop, so no own loop is needed."""

    def __init__(self, pick, icon_path, tooltip):
        global _mac_pick
        self.objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        _mac_pick = pick
        self.app = self.send(self.cls('NSApplication'), 'sharedApplication')
        self.target = self.send(self.send(self._target_class(), 'alloc'), 'init')
        self.bar = self.send(self.cls('NSStatusBar'), 'systemStatusBar')
        item = self.send(self.bar, 'statusItemWithLength:', (ctypes.c_double, -1.0))  # variable length
        self.item = self.send(item, 'retain')
        button = self.send(self.item, 'button')
        image = self.send(self.send(self.cls('NSImage'), 'alloc'), 'initWithContentsOfFile:', self.string(icon_path))
        if image:
            self.send(image, 'setSize:', (_CGSize, _CGSize(18, 18)), restype=None)
            self.send(button, 'setImage:', image, restype=None)
            self.send(image, 'release', restype=None)
        else:
            self.send(button, 'setTitle:', self.string(tooltip), restype=None)
        self.send(button, 'setToolTip:', self.string(tooltip), restype=None)
        self.in_dock = True

    def cls(self, name):
        return self.objc.objc_getClass(name.encode('ascii'))

    def sel(self, name):
        return self.objc.sel_registerName(name.encode('ascii'))

    def send(self, receiver, selector, *args, **options):
        """objc_msgSend with the exact argument types (needed on Apple silicon). An argument is an object
        (int or None) or a pair (ctypes type, value)."""
        pairs = [arg if isinstance(arg, tuple) else (ctypes.c_void_p, arg) for arg in args]
        prototype = ctypes.CFUNCTYPE(options.get('restype', ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
                                     *[kind for kind, _ in pairs])
        function = ctypes.cast(self.objc.objc_msgSend, prototype)
        return function(receiver, self.sel(selector), *[value for _, value in pairs])

    def string(self, text):
        return self.send(self.cls('NSString'), 'stringWithUTF8String:', (ctypes.c_char_p, text.encode('utf-8')))

    def _target_class(self):
        global _mac_target_class, _mac_imp
        if _mac_target_class is None:
            def clicked(receiver, selector, sender):
                if _mac_pick is not None:
                    _mac_pick(self.send(sender, 'tag', restype=ctypes.c_long))

            _mac_imp = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(clicked)
            self.objc.objc_allocateClassPair.restype = ctypes.c_void_p
            self.objc.objc_allocateClassPair.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
            self.objc.class_addMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
            self.objc.objc_registerClassPair.argtypes = [ctypes.c_void_p]
            new = self.objc.objc_allocateClassPair(self.cls('NSObject'), b'CANtrixTrayTarget', 0)
            self.objc.class_addMethod(new, self.sel('pick:'), ctypes.cast(_mac_imp, ctypes.c_void_p), b'v@:@')
            self.objc.objc_registerClassPair(new)
            _mac_target_class = new
        return _mac_target_class

    def set_menu(self, labels):
        menu = self.send(self.send(self.cls('NSMenu'), 'alloc'), 'init')
        for index, label in enumerate(labels):
            if label is None:
                self.send(menu, 'addItem:', self.send(self.cls('NSMenuItem'), 'separatorItem'), restype=None)
                continue
            entry = self.send(self.send(self.cls('NSMenuItem'), 'alloc'), 'initWithTitle:action:keyEquivalent:',
                              self.string(label), self.sel('pick:'), self.string(''))
            self.send(entry, 'setTarget:', self.target, restype=None)
            self.send(entry, 'setTag:', (ctypes.c_long, index), restype=None)
            self.send(menu, 'addItem:', entry, restype=None)
            self.send(entry, 'release', restype=None)
        self.send(self.item, 'setMenu:', menu, restype=None)
        self.send(menu, 'release', restype=None)

    def show_in_dock(self, show):
        """Without a visible window the app lives in the menu bar only; with one it has its Dock icon and
        its menu again."""
        if show != self.in_dock:
            self.in_dock = show
            policy = 0 if show else 1  # NSApplicationActivationPolicyRegular / Accessory
            self.send(self.app, 'setActivationPolicy:', (ctypes.c_long, policy), restype=ctypes.c_bool)
            if show:
                self.activate()

    def activate(self):
        """Bring the app to the front (a window opened from the menu bar would stay behind other apps)."""
        self.send(self.app, 'activateIgnoringOtherApps:', (ctypes.c_bool, True), restype=None)

    def destroy(self):
        global _mac_pick
        _mac_pick = None
        self.show_in_dock(True)
        self.send(self.bar, 'removeStatusItem:', self.item, restype=None)
        self.send(self.item, 'release', restype=None)
        self.send(self.target, 'release', restype=None)


# ---------------------------------------------------------------- Windows

WM_NULL, WM_DESTROY, WM_CLOSE = 0x0000, 0x0002, 0x0010
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY = 0x8000 + 1  # WM_APP + 1: messages of the tray icon
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100


class _WindowsTrayIcon(object):
    """Shell_NotifyIcon with a hidden window and message loop in a thread of its own (no Tk calls there)."""

    def __init__(self, pick, icon_path, tooltip):
        self.pick, self.icon_path, self.tooltip = pick, icon_path, tooltip
        self.labels = []
        self.hwnd = None
        self.error = None
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self.thread.start()
        ready.wait(5)
        if self.hwnd is None or self.error:
            raise OSError(self.error or 'Shell_NotifyIcon')

    def set_menu(self, labels):
        self.labels = list(labels)  # read by the thread when the menu opens

    def show_in_dock(self, show):
        pass  # the taskbar follows the visible windows by itself

    def activate(self):
        pass

    def destroy(self):
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self.thread.join(2)

    def _run(self, ready):
        try:
            self._create()
        except Exception as error:  # reported by the constructor
            self.error = str(error) or error.__class__.__name__
            ready.set()
            return
        ready.set()
        message = self.wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))
        self.user32.UnregisterClassW(self.class_name, self.instance)

    def _create(self):
        from ctypes import wintypes
        self.wintypes = wintypes
        user32 = self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        shell32 = self.shell32 = ctypes.WinDLL('shell32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        result = ctypes.c_ssize_t  # LRESULT
        handle = ctypes.c_void_p
        WNDPROC = ctypes.WINFUNCTYPE(result, handle, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                        ('cbWndExtra', ctypes.c_int), ('hInstance', handle), ('hIcon', handle), ('hCursor', handle),
                        ('hbrBackground', handle), ('lpszMenuName', wintypes.LPCWSTR),
                        ('lpszClassName', wintypes.LPCWSTR)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('hWnd', handle), ('uID', wintypes.UINT),
                        ('uFlags', wintypes.UINT), ('uCallbackMessage', wintypes.UINT), ('hIcon', handle),
                        ('szTip', ctypes.c_wchar * 128), ('dwState', wintypes.DWORD),
                        ('dwStateMask', wintypes.DWORD), ('szInfo', ctypes.c_wchar * 256),
                        ('uVersion', wintypes.UINT), ('szInfoTitle', ctypes.c_wchar * 64),
                        ('dwInfoFlags', wintypes.DWORD), ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', handle)]

        def declare(function, restype, *argtypes):
            function.restype, function.argtypes = restype, list(argtypes)

        declare(kernel32.GetModuleHandleW, handle, wintypes.LPCWSTR)
        declare(user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
        declare(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, handle)
        declare(user32.CreateWindowExW, handle, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, handle, handle, handle, handle)
        declare(user32.DefWindowProcW, result, handle, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        declare(user32.DestroyWindow, wintypes.BOOL, handle)
        declare(user32.PostQuitMessage, None, ctypes.c_int)
        declare(user32.PostMessageW, wintypes.BOOL, handle, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        declare(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), handle, wintypes.UINT, wintypes.UINT)
        declare(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
        declare(user32.DispatchMessageW, result, ctypes.POINTER(wintypes.MSG))
        declare(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
        declare(user32.LoadImageW, handle, handle, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                wintypes.UINT)
        declare(user32.LoadIconW, handle, handle, handle)
        declare(user32.DestroyIcon, wintypes.BOOL, handle)
        declare(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
        declare(user32.CreatePopupMenu, handle)
        declare(user32.AppendMenuW, wintypes.BOOL, handle, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
        declare(user32.DestroyMenu, wintypes.BOOL, handle)
        declare(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
        declare(user32.SetForegroundWindow, wintypes.BOOL, handle)
        declare(user32.TrackPopupMenu, ctypes.c_int, handle, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                handle, handle)
        declare(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))

        self.instance = kernel32.GetModuleHandleW(None)
        self.class_name = 'CANtrixTray%d' % id(self)
        self.wndproc = WNDPROC(self._window_proc)  # keep a reference
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self.wndproc
        window_class.hInstance = self.instance
        window_class.lpszClassName = self.class_name
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        hwnd = user32.CreateWindowExW(0, self.class_name, self.tooltip, 0, 0, 0, 0, 0, None, None, self.instance, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        # IMAGE_ICON from a file (LR_LOADFROMFILE) in the small icon size; otherwise the standard icon.
        self.own_icon = user32.LoadImageW(None, self.icon_path, 1, user32.GetSystemMetrics(49),
                                          user32.GetSystemMetrics(50), 0x0010)
        data = self.data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self.own_icon or user32.LoadIconW(None, 32512)  # IDI_APPLICATION
        data.szTip = self.tooltip
        self.taskbar_created = user32.RegisterWindowMessageW('TaskbarCreated')  # Explorer restarted
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            user32.DestroyWindow(hwnd)
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = hwnd

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_TRAY:
            if lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                self._popup(hwnd)
            return 0
        if message == WM_CLOSE:
            self.user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.data))
            if self.own_icon:
                self.user32.DestroyIcon(self.own_icon)
            self.user32.PostQuitMessage(0)
            return 0
        if self.hwnd and message == self.taskbar_created:
            self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _popup(self, hwnd):
        user32 = self.user32
        menu = user32.CreatePopupMenu()
        for index, label in enumerate(self.labels):
            if label is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, MF_STRING, index + 1, label.replace('&', '&&'))  # 0 means "nothing"
        point = self.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)  # otherwise the menu does not close on a click outside
        picked = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD, point.x, point.y, 0,
                                       hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if picked:
            self.pick(picked - 1)
