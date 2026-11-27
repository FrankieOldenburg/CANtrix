#!/usr/bin/env python3
"""Copy what a widget shows to the clipboard as an image (macOS and Windows).

Tk has no images on the clipboard, so the system functions are called with ctypes: macOS draws the
area of the window into a bitmap (no permission for screen recording needed) and puts a PNG on the
pasteboard, Windows copies the pixels of the widget as a bitmap.

Nur Standardbibliothek, kompatibel mit Python 3.6.
"""
import ctypes
import sys

AVAILABLE = sys.platform in ('darwin', 'win32')


def copy_widget(widget):
    """The visible area of a widget goes to the clipboard. Raises OSError if that is not possible."""
    widget.update()
    if sys.platform == 'darwin':
        _mac_put(_mac_png(widget))
    elif sys.platform == 'win32':
        _windows_copy(widget)
    else:
        raise OSError('Images on the clipboard are only supported on macOS and Windows.')


# ---------------------------------------------------------------- macOS

class _Rect(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double), ('width', ctypes.c_double),
                ('height', ctypes.c_double)]


def _objc():
    library = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    library.objc_getClass.restype = ctypes.c_void_p
    library.objc_getClass.argtypes = [ctypes.c_char_p]
    library.sel_registerName.restype = ctypes.c_void_p
    library.sel_registerName.argtypes = [ctypes.c_char_p]

    def send(receiver, selector, *args, **kwargs):
        """args: (ctypes type, value) pairs; restype: type of the result (default object)."""
        restype = kwargs.get('restype', ctypes.c_void_p)
        function = library.objc_msgSend
        # Structures as results use their own function on Intel Macs.
        if restype is _Rect and ctypes.sizeof(ctypes.c_void_p) == 8 and hasattr(library, 'objc_msgSend_stret'):
            function = library.objc_msgSend_stret
        prototype = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *[kind for kind, _ in args])
        return ctypes.cast(function, prototype)(receiver, library.sel_registerName(selector.encode('ascii')),
                                                *[value for _, value in args])

    def cls(name):
        return library.objc_getClass(name.encode('ascii'))

    def string(text):
        return send(cls('NSString'), 'stringWithUTF8String:', (ctypes.c_char_p, text.encode('utf-8')))

    return send, cls, string


def _mac_view(widget, send, cls, string):
    """Content view of the window of the widget, found by a title set for a moment."""
    top = widget.winfo_toplevel()
    title = top.title()
    marker = 'CANtrix-copy-%x' % id(widget)
    top.title(marker)
    top.update_idletasks()
    try:
        windows = send(send(cls('NSApplication'), 'sharedApplication'), 'windows')
        for index in range(send(windows, 'count', restype=ctypes.c_ulong)):
            window = send(windows, 'objectAtIndex:', (ctypes.c_ulong, index))
            name = send(send(window, 'title'), 'UTF8String', restype=ctypes.c_char_p)
            if name == marker.encode('ascii'):
                return top, send(window, 'contentView')
    finally:
        top.title(title)
    raise OSError('The window of the graph was not found.')


def _mac_png(widget):
    """PNG data (bytes) of the widget as its window draws it."""
    send, cls, string = _objc()
    pool = send(send(cls('NSAutoreleasePool'), 'alloc'), 'init')
    try:
        top, view = _mac_view(widget, send, cls, string)
        width, height = widget.winfo_width(), widget.winfo_height()
        x = widget.winfo_rootx() - top.winfo_rootx()
        y = widget.winfo_rooty() - top.winfo_rooty()
        if not send(view, 'isFlipped', restype=ctypes.c_bool):  # Cocoa counts from the bottom
            y = send(view, 'bounds', restype=_Rect).height - y - height
        area = (_Rect, _Rect(x, y, width, height))
        bitmap = send(view, 'bitmapImageRepForCachingDisplayInRect:', area)
        if not bitmap:
            raise OSError('The graph could not be drawn into an image.')
        send(view, 'cacheDisplayInRect:toBitmapImageRep:', area, (ctypes.c_void_p, bitmap), restype=None)
        data = send(bitmap, 'representationUsingType:properties:', (ctypes.c_ulong, 4),  # PNG
                    (ctypes.c_void_p, send(cls('NSDictionary'), 'dictionary')))
        length = send(data, 'length', restype=ctypes.c_ulong)
        return ctypes.string_at(send(data, 'bytes'), length)
    finally:
        send(pool, 'drain', restype=None)


def _mac_put(png, pasteboard=None):
    """PNG data on the general pasteboard (or the named one, for tests)."""
    send, cls, string = _objc()
    pool = send(send(cls('NSAutoreleasePool'), 'alloc'), 'init')
    try:
        board = send(cls('NSPasteboard'), 'pasteboardWithName:', (ctypes.c_void_p, string(pasteboard))) \
            if pasteboard else send(cls('NSPasteboard'), 'generalPasteboard')
        data = send(cls('NSData'), 'dataWithBytes:length:', (ctypes.c_char_p, png), (ctypes.c_ulong, len(png)))
        send(board, 'clearContents', restype=ctypes.c_long)
        if not send(board, 'setData:forType:', (ctypes.c_void_p, data), (ctypes.c_void_p, string('public.png')),
                    restype=ctypes.c_bool):
            raise OSError('The clipboard did not take the image.')
    finally:
        send(pool, 'drain', restype=None)


# ---------------------------------------------------------------- Windows

def _windows_copy(widget):
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
    user32.GetDC.argtypes, user32.GetDC.restype = [wintypes.HWND], wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes, user32.SetClipboardData.restype = [wintypes.UINT, wintypes.HANDLE], \
        wintypes.HANDLE
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                                                 wintypes.DWORD]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

    handle = widget.winfo_id()
    width, height = widget.winfo_width(), widget.winfo_height()
    source = user32.GetDC(handle)
    memory = gdi32.CreateCompatibleDC(source)
    bitmap = gdi32.CreateCompatibleBitmap(source, width, height)
    try:
        previous = gdi32.SelectObject(memory, bitmap)
        copied = gdi32.BitBlt(memory, 0, 0, width, height, source, 0, 0, 0x00CC0020)  # SRCCOPY
        gdi32.SelectObject(memory, previous)
        if not copied or not user32.OpenClipboard(handle):
            raise OSError('The graph could not be copied.')
        try:
            user32.EmptyClipboard()
            if user32.SetClipboardData(2, bitmap):  # CF_BITMAP; the clipboard owns it now
                bitmap = None
            else:
                raise OSError('The clipboard did not take the image.')
        finally:
            user32.CloseClipboard()
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(handle, source)
