
from __future__ import annotations

import zipfile
from pathlib import Path

__all__ = ["write_sample_gerber"]

_SCALE = 1_000_000


def _c(value: float) -> str:
    return str(int(round(value * _SCALE)))


def _flash(aperture: int, x: float, y: float) -> str:
    return f"D{aperture}*\nX{_c(x)}Y{_c(y)}D03*\n"


def _draw(aperture: int, points: list[tuple[float, float]]) -> str:
    text = f"D{aperture}*\n"
    x, y = points[0]
    text += f"X{_c(x)}Y{_c(y)}D02*\n"
    for x, y in points[1:]:
        text += f"X{_c(x)}Y{_c(y)}D01*\n"
    return text


def _header(function: str) -> str:
    del function
    return (
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        "%LPD*%\n"
        "G01*\n"
        "G90*\n"
    )


def _apertures(definitions: list[str]) -> str:
    return "".join(f"%ADD{line}*%\n" for line in definitions)


def _top_copper() -> str:
    text = _header("Copper,L1,Top")
    text += _apertures(
        [
            "10C,0.250000",
            "11C,1.500000",
            "12C,0.400000",
            "13R,2.000000X1.000000",
        ]
    )
    for y in (6.0, 12.0, 18.0, 24.0, 30.0, 34.0):
        text += _draw(10, [(6.0, y), (54.0, y)])
    for x in (6.0, 18.0, 30.0, 42.0, 54.0):
        text += _draw(10, [(x, 6.0), (x, 34.0)])
    for x in (10.0, 24.0, 38.0, 50.0):
        for y in (9.0, 21.0, 33.0):
            text += _flash(11, x, y)
    text += _flash(13, 30.0, 16.0)
    text += _flash(13, 30.0, 26.0)
    text += _draw(12, [(8.0, 15.0), (8.0, 27.0), (20.0, 27.0)])
    text += "M02*\n"
    return text


def _bottom_copper() -> str:
    text = _header("Copper,L2,Bot")
    text += _apertures(["10C,0.250000", "11C,1.500000", "12C,0.400000"])
    for y in (9.0, 15.0, 21.0, 27.0, 33.0):
        text += _draw(10, [(8.0, y), (52.0, y)])
    for x in (14.0, 26.0, 38.0, 46.0):
        text += _draw(10, [(x, 9.0), (x, 33.0)])
    for x in (12.0, 28.0, 44.0):
        for y in (12.0, 24.0):
            text += _flash(11, x, y)
    text += _draw(12, [(16.0, 12.0), (40.0, 12.0)])
    text += "M02*\n"
    return text


def _outline() -> str:
    text = _header("Profile,NP")
    text += _apertures(["10C,0.150000"])
    text += _draw(
        10,
        [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0), (0.0, 0.0)],
    )
    text += "M02*\n"
    return text


def _top_solder_mask() -> str:
    text = _header("Soldermask,Top")
    text += _apertures(["10C,1.700000", "11R,2.200000X1.200000"])
    for x in (10.0, 24.0, 38.0, 50.0):
        for y in (9.0, 21.0, 33.0):
            text += _flash(10, x, y)
    text += _flash(11, 30.0, 16.0)
    text += _flash(11, 30.0, 26.0)
    text += "M02*\n"
    return text


def _bottom_solder_mask() -> str:
    text = _header("Soldermask,Bot")
    text += _apertures(["10C,1.700000"])
    for x in (12.0, 28.0, 44.0):
        for y in (12.0, 24.0):
            text += _flash(10, x, y)
    text += "M02*\n"
    return text


def _silkscreen() -> str:
    text = _header("Legend,Top")
    text += _apertures(["10C,0.150000"])
    text += _draw(10, [(4.0, 37.0), (56.0, 37.0), (56.0, 3.0), (4.0, 3.0), (4.0, 37.0)])
    text += _draw(10, [(28.0, 10.0), (28.0, 30.0)])
    text += _draw(10, [(20.0, 32.0), (40.0, 32.0)])
    text += "M02*\n"
    return text


def _coord(value: float) -> str:
    whole = int(value)
    fraction = int(round((value - whole) * 1000))
    if fraction >= 1000:
        whole += 1
        fraction -= 1000
    return f"{whole:03d}.{fraction:03d}"


def _drill(tools: list[tuple[int, float, list[tuple[float, float]]]]) -> str:
    text = "M48\n;FILE_FORMAT=3:3\nMETRIC,LZ,000.000\n"
    for number, diameter, _points in tools:
        text += f"T{number}C{diameter:.3f}\n"
    text += "%\nG90\nG05\n"
    for number, _diameter, points in tools:
        text += f"T{number}\n"
        for x, y in points:
            text += f"X{_coord(x)}Y{_coord(y)}\n"
    text += "M30\n"
    return text


def _pth_drill() -> str:
    return _drill(
        [
            (1, 0.400, [(10.0, 9.0), (24.0, 9.0), (38.0, 9.0), (50.0, 9.0)]),
            (2, 0.800, [(10.0, 21.0), (24.0, 21.0), (38.0, 21.0), (50.0, 21.0)]),
            (3, 1.000, [(10.0, 33.0), (24.0, 33.0), (38.0, 33.0), (50.0, 33.0)]),
            (4, 1.200, [(12.0, 12.0), (44.0, 12.0)]),
            (5, 1.500, [(28.0, 24.0)]),
        ]
    )


def _npth_drill() -> str:
    return _drill([(1, 3.000, [(30.0, 20.0), [(6.0, 36.0)][0]])])


def write_sample_gerber(directory: str | Path) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    files = {
        "Sample_TopLayer.GTL": _top_copper(),
        "Sample_BottomLayer.GBL": _bottom_copper(),
        "Sample_BoardOutlineLayer.GKO": _outline(),
        "Sample_TopSilkscreenLayer.GTO": _silkscreen(),
        "Sample_TopSolderMaskLayer.GTS": _top_solder_mask(),
        "Sample_BottomSolderMaskLayer.GBS": _bottom_solder_mask(),
        "Drill_PTH_Through.DRL": _pth_drill(),
        "Drill_NPTH_Through.DRL": _npth_drill(),
    }
    for name, text in files.items():
        (target / name).write_text(text, encoding="utf-8", newline="\n")

    archive = target / "sample_gerber.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for name in files:
            handle.write(target / name, name)
    return archive
