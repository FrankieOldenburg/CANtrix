#!/usr/bin/env python3
"""Trixie: frei platzierbarer CANtrix-Statushelfer."""
import os
import sys

try:
    import tkinter as tk
except ImportError:
    tk = None

from CAN_Common import resource_dir
from CAN_Language import tr


if tk is not None:

    class Trixie(object):
        """Eigenständiges, animiertes Overlay mit Status-Sprechblase."""
        def __init__(self, parent, status_var):
            self.parent, self.status_var = parent, status_var
            self.window = tk.Toplevel(parent)
            self.window.withdraw()
            self.window.overrideredirect(True)
            self.window.attributes('-topmost', True)
            self._background = '#10192d'
            if sys.platform == 'darwin':
                # Tk Aqua paints transparent Toplevel content black on some builds.
                # Match CANtrix's dark surface so the mascot remains visible.
                self.window.configure(background=self._background)
                try:
                    self.window.tk.call('::tk::unsupported::MacWindowStyle', 'style', self.window,
                                        'utility', 'noActivates')
                except tk.TclError:
                    pass
            else:
                self._transparent = '#ff00ff'
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
            image_path = os.path.join(resource_dir(), 'Bilder', 'Trixie-CANtrix.png')
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
            if not self.window.winfo_exists():
                return
            self.bubble_text = text or tr('Alles ruhig in der CAN-Galaxis!')
            self.canvas.itemconfigure(self._bubble_label, text=self.bubble_text)
            if self.window.wm_state() != 'withdrawn':
                self.window.attributes('-topmost', True)
                self.window.lift()

        def show(self):
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
            self.window.withdraw()

        def _drag_start(self, event):
            self._drag = (event.x_root - self.window.winfo_x(), event.y_root - self.window.winfo_y())

        def _drag_move(self, event):
            if self._drag:
                self.window.geometry('+%d+%d' % (event.x_root - self._drag[0], event.y_root - self._drag[1]))
