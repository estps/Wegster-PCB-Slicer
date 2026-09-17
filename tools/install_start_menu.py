
from __future__ import annotations

import argparse
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICON = ASSETS / "wegstr.ico"
SHORTCUT_NAME = "Wegstr PCB Slicer.lnk"

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_icon(size: int):
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    scale = size / 256.0

    def px(value: float) -> float:
        return value * scale

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#12121A"))
    painter.drawRoundedRect(QRectF(px(6), px(6), px(244), px(244)), px(46), px(46))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#2E2E3E"), px(7)))
    painter.drawRoundedRect(QRectF(px(6), px(6), px(244), px(244)), px(46), px(46))

    painter.setPen(QPen(QColor("#F38BA8"), px(11)))
    painter.drawRoundedRect(QRectF(px(42), px(38), px(172), px(180)), px(18), px(18))

    cap = Qt.PenCapStyle.RoundCap
    join = Qt.PenJoinStyle.RoundJoin

    trace_a = QPainterPath()
    trace_a.moveTo(px(80), px(74))
    trace_a.lineTo(px(80), px(142))
    trace_a.lineTo(px(142), px(142))
    trace_a.lineTo(px(142), px(192))
    painter.setPen(QPen(QColor("#A6E3A1"), px(10), Qt.PenStyle.SolidLine, cap, join))
    painter.drawPath(trace_a)

    trace_b = QPainterPath()
    trace_b.moveTo(px(176), px(74))
    trace_b.lineTo(px(176), px(116))
    trace_b.lineTo(px(112), px(116))
    trace_b.lineTo(px(112), px(192))
    painter.setPen(QPen(QColor("#89B4FA"), px(10), Qt.PenStyle.SolidLine, cap, join))
    painter.drawPath(trace_b)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#FAB387"))
    for x, y in ((80, 74), (142, 192), (176, 74), (112, 192)):
        painter.drawEllipse(QPointF(px(x), px(y)), px(15), px(15))

    painter.end()
    return image


def _png_bytes(image) -> bytes:
    from PySide6.QtCore import QBuffer

    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def write_icon(path: Path) -> None:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])

    frames = [draw_icon(size) for size in ICON_SIZES]
    blobs = [_png_bytes(frame) for frame in frames]

    offset = 6 + 16 * len(frames)
    directory = b""
    for frame, blob in zip(frames, blobs):
        width = 0 if frame.width() >= 256 else frame.width()
        height = 0 if frame.height() >= 256 else frame.height()
        directory += struct.pack(
            "<BBBBHHII", width, height, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, len(frames)))
        handle.write(directory)
        for blob in blobs:
            handle.write(blob)
    del app


def start_menu_dir() -> Path:
    return Path(
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "[Environment]::GetFolderPath('Programs')",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )


def install() -> int:
    if not ICON.exists():
        print(f"Generating {ICON.relative_to(ROOT)} …")
        write_icon(ICON)
    print(f"  icon: {ICON} ({ICON.stat().st_size / 1024:.1f} KB)")

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.shortcuts import current_target, install_shortcut

    target = current_target()
    if target is None:
        print("error: could not work out what to point the shortcut at", file=sys.stderr)
        return 2

    shortcut = install_shortcut(target=target)
    if shortcut is None:
        print("error: shortcut was not created", file=sys.stderr)
        return 1

    print(f"  shortcut: {shortcut}")
    print()
    print("Installed. Search the Start menu for 'Wegstr' to launch it.")
    return 0


def remove() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.shortcuts import remove_shortcut, shortcut_path

    path = shortcut_path()
    if remove_shortcut():
        print(f"Removed {path}")
    else:
        print("Nothing to remove.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remove", action="store_true", help="remove the Start-menu shortcut"
    )
    parser.add_argument(
        "--icon-only", action="store_true", help="just regenerate the .ico"
    )
    args = parser.parse_args(argv)

    if args.remove:
        return remove()
    if args.icon_only:
        write_icon(ICON)
        print(f"Wrote {ICON}")
        return 0
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
