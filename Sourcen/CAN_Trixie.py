#!/usr/bin/env python3
"""Trixie: frei platzierbarer CANtrix-Statushelfer."""
import os
import sys
import ctypes

try:
    import tkinter as tk
except ImportError:
    tk = None

from CAN_Common import resource_dir
from CAN_Language import tr


class _CGPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [('origin', _CGPoint), ('size', _CGSize)]


_mac_draw_class = None
_mac_draw_callback = None


class _MacTrixieOverlay(object):
    """Native transparent NSPanel on macOS; Tk Aqua paints transparent canvases black."""
    def __init__(self, parent, image_path):
        self.parent = parent
        self.visible = False
        self._bob = False
        self.objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self.panel = self.send(self.send(self.cls('NSPanel'), 'alloc'), 'initWithContentRect:styleMask:backing:defer:',
                               (_CGRect, _CGRect(_CGPoint(0, 0), _CGSize(340, 310))),
                               (ctypes.c_ulong, 0), (ctypes.c_ulong, 2), (ctypes.c_bool, False))
        self.send(self.panel, 'setOpaque:', (ctypes.c_bool, False), restype=None)
        self.send(self.panel, 'setBackgroundColor:', self.send(self.cls('NSColor'), 'clearColor'), restype=None)
        self.send(self.panel, 'setHasShadow:', (ctypes.c_bool, False), restype=None)
        self.send(self.panel, 'setLevel:', (ctypes.c_long, 3), restype=None)  # floating window level
        self.send(self.panel, 'setMovableByWindowBackground:', (ctypes.c_bool, True), restype=None)
        self.send(self.panel, 'setHidesOnDeactivate:', (ctypes.c_bool, False), restype=None)
        self.send(self.panel, 'setReleasedWhenClosed:', (ctypes.c_bool, False), restype=None)

        self.content = self.send(self.send(self.cls('NSView'), 'alloc'), 'initWithFrame:',
                                 (_CGRect, _CGRect(_CGPoint(0, 0), _CGSize(340, 310))))
        self.send(self.panel, 'setContentView:', self.content, restype=None)
        self.bubble = self.send(self.send(self._draw_view_class(), 'alloc'), 'initWithFrame:',
                                (_CGRect, _CGRect(_CGPoint(0, 180), _CGSize(340, 120))))
        self.send(self.content, 'addSubview:', self.bubble, restype=None)
        self.text = self.send(self.send(self.cls('NSTextField'), 'alloc'), 'initWithFrame:',
                              (_CGRect, _CGRect(_CGPoint(26, 38), _CGSize(288, 62))))
        for selector in ('setBezeled:', 'setDrawsBackground:', 'setEditable:', 'setSelectable:'):
            self.send(self.text, selector, (ctypes.c_bool, False), restype=None)
        self.send(self.text, 'setAlignment:', (ctypes.c_long, 2), restype=None)
        self.send(self.text, 'setTextColor:', self.send(self.cls('NSColor'), 'colorWithCalibratedRed:green:blue:alpha:',
                    (ctypes.c_double, 38 / 255.0), (ctypes.c_double, 50 / 255.0),
                    (ctypes.c_double, 56 / 255.0), (ctypes.c_double, 1.0)), restype=None)
        cell = self.send(self.text, 'cell')
        self.send(cell, 'setWraps:', (ctypes.c_bool, True), restype=None)
        self.send(cell, 'setUsesSingleLineMode:', (ctypes.c_bool, False), restype=None)
        font = self.send(self.cls('NSFont'), 'boldSystemFontOfSize:', (ctypes.c_double, 17.0))
        self.send(self.text, 'setFont:', font, restype=None)
        self.send(self.bubble, 'addSubview:', self.text, restype=None)

        image = self.send(self.send(self.cls('NSImage'), 'alloc'), 'initWithContentsOfFile:', self.string(image_path))
        if not image:
            raise OSError('Trixie.png konnte nicht geladen werden: ' + image_path)
        self.image_view = self.send(self.send(self.cls('NSImageView'), 'alloc'), 'initWithFrame:',
                                    (_CGRect, _CGRect(_CGPoint(77, 2), _CGSize(186, 167))))
        self.send(self.image_view, 'setImage:', image, restype=None)
        self.send(self.image_view, 'setImageScaling:', (ctypes.c_long, 3), restype=None)
        self.send(self.content, 'addSubview:', self.image_view, restype=None)
        self.send(image, 'release', restype=None)
        self.send(self.panel, 'orderOut:', None, restype=None)
        self._animation_id = None

    def cls(self, name):
        return self.objc.objc_getClass(name.encode('ascii'))

    def sel(self, name):
        return self.objc.sel_registerName(name.encode('ascii'))

    def send(self, receiver, selector, *args, **options):
        pairs = [arg if isinstance(arg, tuple) else (ctypes.c_void_p, arg) for arg in args]
        prototype = ctypes.CFUNCTYPE(options.get('restype', ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
                                     *[kind for kind, _ in pairs])
        function = ctypes.cast(self.objc.objc_msgSend, prototype)
        return function(receiver, self.sel(selector), *[value for _, value in pairs])

    def string(self, value):
        return self.send(self.cls('NSString'), 'stringWithUTF8String:',
                         (ctypes.c_char_p, value.encode('utf-8')))

    def _draw_view_class(self):
        global _mac_draw_class, _mac_draw_callback
        if _mac_draw_class is None:
            def draw_rect(view, selector, rect):
                try:
                    cream = self.send(self.cls('NSColor'), 'colorWithCalibratedRed:green:blue:alpha:',
                                      (ctypes.c_double, 1.0), (ctypes.c_double, 0.98),
                                      (ctypes.c_double, 0.94), (ctypes.c_double, 1.0))
                    gold = self.send(self.cls('NSColor'), 'colorWithCalibratedRed:green:blue:alpha:',
                                     (ctypes.c_double, 0.83), (ctypes.c_double, 0.61),
                                     (ctypes.c_double, 0.33), (ctypes.c_double, 1.0))
                    path = self.send(self.cls('NSBezierPath'), 'bezierPathWithRoundedRect:xRadius:yRadius:',
                                     (_CGRect, _CGRect(_CGPoint(12, 24), _CGSize(316, 82))),
                                     (ctypes.c_double, 18.0), (ctypes.c_double, 18.0))
                    self.send(cream, 'setFill', restype=None)
                    self.send(path, 'fill', restype=None)
                    tail = self.send(self.cls('NSBezierPath'), 'bezierPath')
                    self.send(tail, 'moveToPoint:', (_CGPoint, _CGPoint(140, 24)), restype=None)
                    self.send(tail, 'lineToPoint:', (_CGPoint, _CGPoint(170, 0)), restype=None)
                    self.send(tail, 'lineToPoint:', (_CGPoint, _CGPoint(200, 24)), restype=None)
                    self.send(tail, 'closePath', restype=None)
                    self.send(cream, 'setFill', restype=None)
                    self.send(tail, 'fill', restype=None)
                    self.send(gold, 'setStroke', restype=None)
                    self.send(path, 'setLineWidth:', (ctypes.c_double, 3.0), restype=None)
                    self.send(tail, 'setLineWidth:', (ctypes.c_double, 3.0), restype=None)
                    self.send(path, 'stroke', restype=None)
                    self.send(tail, 'stroke', restype=None)
                except Exception:
                    pass

            _mac_draw_callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, _CGRect)(draw_rect)
            self.objc.objc_allocateClassPair.restype = ctypes.c_void_p
            self.objc.objc_allocateClassPair.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
            self.objc.class_addMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
            self.objc.objc_registerClassPair.argtypes = [ctypes.c_void_p]
            new_class = self.objc.objc_allocateClassPair(self.cls('NSView'), b'CANtrixTrixieBubbleView', 0)
            self.objc.class_addMethod(new_class, self.sel('drawRect:'),
                                      ctypes.cast(_mac_draw_callback, ctypes.c_void_p),
                                      b'v@:{CGRect={CGPoint=dd}{CGSize=dd}}')
            self.objc.objc_registerClassPair(new_class)
            _mac_draw_class = new_class
        return _mac_draw_class

    def set_text(self, text):
        self.send(self.text, 'setStringValue:', self.string(text), restype=None)
        self.send(self.bubble, 'setNeedsDisplay:', (ctypes.c_bool, True), restype=None)

    def _set_origin(self, x, y):
        self.send(self.panel, 'setFrameOrigin:', (_CGPoint, _CGPoint(x, y)), restype=None)

    def show(self):
        self.visible = True
        screen_w = self.parent.winfo_screenwidth()
        x = max(0, screen_w - 340 - 24)
        self._set_origin(x, 24)
        self.send(self.panel, 'orderFrontRegardless', restype=None)
        self._animate()

    def _animate(self):
        if not self.visible:
            return
        self._bob = not self._bob
        y = 2 if self._bob else 5
        self.send(self.image_view, 'setFrameOrigin:', (_CGPoint, _CGPoint(77, y)), restype=None)
        self._animation_id = self.parent.after(650, self._animate)

    def hide(self):
        self.visible = False
        if self._animation_id:
            try:
                self.parent.after_cancel(self._animation_id)
            except Exception:
                pass
            self._animation_id = None
        self.send(self.panel, 'orderOut:', None, restype=None)


if tk is not None:

    class Trixie(object):
        """Eigenständiges, animiertes Overlay mit Status-Sprechblase."""
        def __init__(self, parent, status_var):
            self.parent, self.status_var = parent, status_var
            self._native = None
            if sys.platform == 'darwin':
                image_path = os.path.join(resource_dir(), 'Bilder', 'Trixie.png')
                self._native = _MacTrixieOverlay(parent, image_path)
                self._native.set_text(tr('Hallo! Ich bin Trixie, deine CAN-Koboldin.'))
                status_var.trace_add('write', self._status_changed)
                return
            self.window = tk.Toplevel(parent)
            self.window.withdraw()
            self.window.overrideredirect(True)
            self.window.attributes('-topmost', True)
            self._transparent = '#ff00ff'
            self._background = self._transparent
            self.window.configure(background=self._transparent)
            try:
                self.window.attributes('-transparentcolor', self._transparent)
            except tk.TclError:
                self._background = '#10192d'
                self.window.configure(background=self._background)

            self.window.title('Trixie')
            self.canvas = tk.Canvas(self.window, width=320, height=108, background=self._background,
                                    highlightthickness=0, cursor='fleur')
            self.canvas.pack(padx=2, pady=2)
            self.bubble_text = tr('Hallo! Ich bin Trixie, deine CAN-Koboldin.')
            self._draw_bubble()
            image_path = os.path.join(resource_dir(), 'Bilder', 'Trixie.png')
            self.trixie_image = tk.PhotoImage(master=self.window, file=image_path).subsample(7, 7)
            self.figure_canvas = tk.Canvas(self.window, width=320, height=180, background=self._background,
                                           highlightthickness=0, cursor='fleur')
            self.figure_canvas.pack(padx=2, pady=(0, 2))
            self._image = self.figure_canvas.create_image(160, 90, image=self.trixie_image, tags=('trixie',))
            self.window.bind('<ButtonPress-1>', self._drag_start)
            self.window.bind('<B1-Motion>', self._drag_move)
            self.window.bind('<Double-Button-1>', lambda event: self.hide())
            self._drag = None
            self._bob = False
            self._animation_id = None
            self.window.geometry('+80+100')
            status_var.trace_add('write', self._status_changed)
            self._animate()

        def _draw_bubble(self):
            c = self.canvas
            fill, edge = '#fffaf0', '#d49b53'
            c.create_polygon(22, 8, 278, 8, 286, 10, 290, 18, 290, 66, 286, 74,
                             278, 78, 22, 78, 14, 74, 10, 66, 10, 20, 14, 12,
                             fill=fill, outline=edge, width=2, smooth=True, splinesteps=12, tags=('bubble',))
            c.create_polygon(141, 75, 158, 96, 176, 75, fill=fill, outline=edge, width=2,
                             joinstyle=tk.ROUND, tags=('bubble',))
            self._bubble_label = c.create_text(150, 43, text=self.bubble_text, width=245, justify=tk.CENTER,
                                               fill='#263238', font=('Helvetica', 11, 'bold'), tags=('bubble',))

        def _animate(self):
            if not self.window.winfo_exists():
                return
            self._bob = not self._bob
            self.figure_canvas.move(self._image, 0, -3 if self._bob else 3)
            self.figure_canvas.tag_raise('trixie')
            if self.window.wm_state() != 'withdrawn':
                self.window.attributes('-topmost', True)
            self._animation_id = self.window.after(650, self._animate)

        def _status_changed(self, *_):
            try:
                self.say(self.status_var.get())
            except tk.TclError:
                pass

        def say(self, text):
            if self._native is not None:
                self._native.set_text(text or tr('Alles ruhig in der CAN-Galaxis!'))
                return
            if not self.window.winfo_exists():
                return
            self.bubble_text = text or tr('Alles ruhig in der CAN-Galaxis!')
            self.canvas.itemconfigure(self._bubble_label, text=self.bubble_text)
            if self.window.wm_state() != 'withdrawn':
                self.window.attributes('-topmost', True)
                self.window.lift()

        def show(self):
            if self._native is not None:
                self._native.show()
                return
            self.window.update_idletasks()
            width = self.window.winfo_reqwidth()
            height = self.window.winfo_reqheight()
            x = max(0, self.window.winfo_screenwidth() - width - 24)
            y = max(0, self.window.winfo_screenheight() - height - 48)
            self.window.geometry('+%d+%d' % (x, y))
            self.window.deiconify()
            self.window.attributes('-topmost', True)
            self.window.lift()

        def hide(self):
            if self._native is not None:
                self._native.hide()
                return
            self.window.withdraw()

        def _drag_start(self, event):
            self._drag = (event.x_root - self.window.winfo_x(), event.y_root - self.window.winfo_y())

        def _drag_move(self, event):
            if self._drag:
                self.window.geometry('+%d+%d' % (event.x_root - self._drag[0], event.y_root - self._drag[1]))
