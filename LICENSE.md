# License

## CANtrix

MIT License

Copyright (c) 2026 Frank Hoheisel

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

This license covers the source code, the documentation, the images in
`Bilder/` and `Screenshots/` and the demo files (`Database/Demo/`, `Traces/Demo/`, `Panel/Demo/`, `Nodes/Demo/`).

## Third-party components

CANtrix itself uses only the Python standard library. The following
components are not part of this repository; they are installed separately or
added when the program is packaged.

### python-can

- Project: <https://github.com/hardbyte/python-can>
- License: GNU Lesser General Public License, version 3 (LGPL-3.0)
  (<https://www.gnu.org/licenses/lgpl-3.0.html>)
- Use: optional, only for the data source "Live" (recording with a Vector
  interface). It is installed by the user (`pip install python-can`) and
  imported at run time. It is not modified.
- Packaged program: the build scripts `Batch/03 - make exe_…` include python-can
  in `CANtrix.exe` / `CANtrix.app` (`--collect-all can`). Whoever
  distributes such a build must comply with the LGPL-3.0 for python-can:
  include the LGPL-3.0 license text and the copyright notice of python-can,
  and point to its source code (link above). Because python-can is a
  separate, replaceable library, the rest of CANtrix stays under the MIT
  license.

### Python and Tcl/Tk

- License: Python Software Foundation License (<https://docs.python.org/3/license.html>)
  and the Tcl/Tk license (<https://www.tcl.tk/software/tcltk/license.html>).
- Use: runtime of the program. A packaged build (`CANtrix.exe` /
  `.app`, made with PyInstaller) contains the Python interpreter and Tcl/Tk;
  their license terms apply to these parts.

### Vector XL Driver Library

- Not included. Recording with a Vector interface (e.g. VN1640A) needs the
  driver (`vxlapi64.dll`) of Vector Informatik GmbH, which the user installs
  under Vector's own license terms.

## Trademarks

Vector, CANalyzer, CANoe, VN1640A, PEAK, PCAN-View, ASAM, BUSMASTER and other
product names are used only to name the file formats and devices the program
works with. They belong to their respective owners. CANtrix is not
affiliated with them.
