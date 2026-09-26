# How it works, and why it has to work this way

The trick is not a style: a window that has no background is not a CSS question, and the two
obvious answers both fail. This is the record of what was tried, so nobody tries it again.

## What works: per-pixel alpha, with the face drawn by us

`surface.py` draws the face into a 32-bit image with pillow — the time, the date, a blurred halo
under both, and the gear when the pointer is over it. `app.py` hands that image to Windows with
**`UpdateLayeredWindow`**, so the window *is* that image:

* an unpainted pixel is not a colour, and not a copy of the wallpaper: it is the wallpaper.
  Windows composites the image over the desktop one pixel at a time, so anything moving behind
  the widget keeps moving;
* the glyph edges are antialiased toward nothing, so they sit on the wallpaper cleanly instead
  of on a background that has to be matched;
* only painted pixels are hit-tested. The block the text occupies carries an alpha of **1**
  (0.4%) — Rainmeter's `SolidColor=0,0,0,1` — plus a small square in the bottom-right corner.
  Those are the parts you can drag, resize and click; everywhere else, a click goes to whatever
  is underneath the widget.

The face is redrawn on the minute, on hover, on ink changes and on resize. The ink comes from
the brightness of the wallpaper sampled in a ring just *outside* the window (inside it, the
pixels are the widget's own): dark ink over something bright, light ink over something dark, and
a dead zone between the two so a passing window cannot make the text flicker. The halo flips
with it.

## What does not work, and how it was measured

**1. Painting a capture of the desktop behind itself.** `BitBlt` the screen into a PNG, keep the
window out of captures with `SetWindowDisplayAffinity`, and let the page draw the image at
`-(x, y)`. Pixel-perfect, and wrong: anything that moves behind the widget is frozen into the
copy — taskbar thumbnails appear *inside* the clock — and the window's own surface shows as
black edges wherever the copy and the window disagree by a pixel.

**2. Keying one exact colour out of the window** — `WS_EX_LAYERED` with
`SetLayeredWindowAttributes(hwnd, key, 0, LWA_COLORKEY)`, the classic Win32 answer to "text with
no box". It does not work with WebView2, which composites its own output: **every pixel of the
window stays painted whatever key is set.** Measured with a control that cannot be fooled —
photograph the window's rectangle, kill the process, photograph the same rectangle, and count
the pixels that differ — twice: 59,700 of 59,700 pixels, then 45,000 of 45,000, still painted.
Keying the frame *and every child window* (`Chrome_WidgetWin_0/1`,
`Chrome_RenderWidgetHostHWND`, `Intermediate D3D Window`) changed nothing. Combining the key
with `LWA_ALPHA` is worse, not better: alpha 254 stops the key acting at all.

A *transparent* page does not help either. pywebview's `transparent=True` only clears the
webview's own background; the window it sits in has no alpha of its own, and the desktop never
shows through it.

That is why there is no browser in this widget at all: no pywebview, no HTML, no CSS, no
JavaScript, no bridge, and no `ui/` folder. Two lines of text do not need one.

## Paper cuts worth remembering

* `CreateWindowExW` leaves a `WS_POPUP` window **hidden**. `WS_VISIBLE` has to be in the style,
  and without it the widget simply shows nothing at all — not even a black box.
* Never call `SetLayeredWindowAttributes` on a window you also paint with `UpdateLayeredWindow`:
  one call, for an alpha the image already carries, makes every later `UpdateLayeredWindow` fail
  **without a word**, and the window keeps its uninitialised black surface.
* Dragging via `HTCAPTION` gives Windows' own smooth window drag in five lines, but the mouse
  movement over that block then arrives as **`WM_NCMOUSEMOVE`**, and `TrackMouseEvent` with
  `TME_NONCLIENT` reports a leave immediately on a window with no frame. The hover state is
  therefore decided on the timer, from `GetCursorPos`.
* A resize gesture must measure from the size it *started* at. Growing from the previous step
  compounds: a 60-pixel drag became a 672-pixel resize.
* A resize handle that only exists in the *hover* render is not there when you reach for it: the
  press lands on the desktop (alpha 0) and nothing happens. It is drawn in every render.
* For **Stay on the desktop**, the window procedure refuses `SC_MINIMIZE` — which is what Show
  Desktop sends — and the widget borrows topmost *only while the desktop is in front*, handing
  it back (and going to the bottom) as soon as the windows return. Asking "is the desktop in
  front?" is reliable; asking "is the widget covered?" flaps, because lifting the widget is what
  un-covers it.
