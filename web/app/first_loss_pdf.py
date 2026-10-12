"""Searchable English PDF from a completed snapshot, using only the stdlib.

No job, network request, scientific dependency or raw-killmail query is needed
to export. Original user-supplied cover artwork is embedded without alteration.
"""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import struct
import zlib

ASSETS = Path(__file__).with_name("report_assets")
WIDTHS = json.loads((ASSETS / "font_widths.json").read_text())
NAVY = (0.06, 0.10, 0.16)
RED = (0.74, 0.12, 0.16)
GREEN = (0.10, 0.43, 0.30)
INK = (0.13, 0.18, 0.24)
GRAY = (0.40, 0.46, 0.53)


def ascii_text(value):
    return (
        str(value if value is not None else "Unavailable")
        .translate(
            str.maketrans(
                {
                    "→": " -> ",
                    "×": " x ",
                    "−": "-",
                    "–": "-",
                    "—": "-",
                    "≥": ">=",
                    "≤": "<=",
                    "’": "'",
                    "…": "...",
                }
            )
        )
        .encode("cp1252", "replace")
        .decode("cp1252")
    )


def literal(value):
    value = (
        ascii_text(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    )
    return "(" + value.replace("\n", " ").replace("\r", " ") + ")"


def width(value, size=10, bold=False):
    table = WIDTHS[bool(bold)]
    return (
        sum(
            table[ord(c) - 32] if 32 <= ord(c) <= 126 else 600
            for c in ascii_text(value)
        )
        * size
        / 1000
    )


def wrap(value, maximum, size=10, bold=False):
    lines = []
    for paragraph in ascii_text(value).splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            if line and width(line + " " + word, size, bold) > maximum:
                lines.append(line)
                line = ""
            while width(word, size, bold) > maximum:
                split = 1
                while (
                    split < len(word)
                    and width(word[: split + 1], size, bold) <= maximum
                ):
                    split += 1
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:split])
                word = word[split:]
            line = line + " " + word if line else word
        lines.append(line)
    return lines


def png_planes(path):
    """Decode the bundled noninterlaced RGB/RGBA PNG, retaining its alpha."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid cover PNG")
    offset, payload = 8, bytearray()
    while offset < len(data):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        kind, chunk = (
            data[offset + 4 : offset + 8],
            data[offset + 8 : offset + 8 + size],
        )
        if kind == b"IHDR":
            w, h, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if (
                depth != 8
                or color not in (2, 6)
                or compression
                or filtering
                or interlace
            ):
                raise ValueError("Unsupported cover PNG format")
        elif kind == b"IDAT":
            payload.extend(chunk)
        offset += 12 + size
    raw = zlib.decompress(payload)
    channels, rgb, alpha = (4 if color == 6 else 3), bytearray(), bytearray()
    stride, previous, cursor = w * channels, bytearray(w * channels), 0
    for _ in range(h):
        mode = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        for i in range(stride):
            left = row[i - channels] if i >= channels else 0
            above = previous[i]
            upper_left = previous[i - channels] if i >= channels else 0
            if mode == 1:
                predictor = left
            elif mode == 2:
                predictor = above
            elif mode == 3:
                predictor = (left + above) // 2
            elif mode == 4:
                p = left + above - upper_left
                differences = (abs(p - left), abs(p - above), abs(p - upper_left))
                predictor = (left, above, upper_left)[
                    differences.index(min(differences))
                ]
            elif mode == 0:
                predictor = 0
            else:
                raise ValueError("Invalid cover PNG filter")
            row[i] = (row[i] + predictor) & 255
        for i in range(0, stride, channels):
            rgb.extend(row[i : i + 3])
            alpha.append(row[i + 3] if channels == 4 else 255)
        previous = row
    return w, h, bytes(rgb), bytes(alpha)


class PDF:
    W, H, M = 842, 595, 38

    def __init__(self):
        self.pages = []
        self.page = None
        self.y = 0

    def command(self, value):
        self.page.append(value)

    def text(self, x, y, value, size=10, bold=False, color=INK):
        self.command(
            f"BT /{'F2' if bold else 'F1'} {size} Tf {' '.join(map(str, color))} rg 1 0 0 1 {x:.2f} {y:.2f} Tm {literal(value)} Tj ET"
        )

    def rect(self, x, y, w, h, color):
        self.command(
            f"{' '.join(map(str, color))} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f"
        )

    def line(self, x1, y1, x2, y2, color=GRAY):
        self.command(
            f"{' '.join(map(str, color))} RG 0.8 w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S"
        )

    def circle(self, x, y, radius, color, fill=False):
        k = radius * 0.55228475
        self.command(
            f"{' '.join(map(str, color))} {'rg' if fill else 'RG'} {x+radius:.2f} {y:.2f} m "
            f"{x+radius:.2f} {y+k:.2f} {x+k:.2f} {y+radius:.2f} {x:.2f} {y+radius:.2f} c "
            f"{x-k:.2f} {y+radius:.2f} {x-radius:.2f} {y+k:.2f} {x-radius:.2f} {y:.2f} c "
            f"{x-radius:.2f} {y-k:.2f} {x-k:.2f} {y-radius:.2f} {x:.2f} {y-radius:.2f} c "
            f"{x+k:.2f} {y-radius:.2f} {x+radius:.2f} {y-k:.2f} {x+radius:.2f} {y:.2f} c {'f' if fill else 'S'}"
        )

    def new_page(self, title="First Loss and Observed Return", cover=False):
        self.page = []
        self.pages.append(self.page)
        self.y = self.H - 76
        if not cover:
            self.rect(0, self.H - 48, self.W, 48, NAVY)
            self.text(self.M, self.H - 30, title, 13, True, (1, 1, 1))
            self.text(
                self.W - 133, self.H - 29, "CLASSIFIED", 11, True, (1, 0.35, 0.35)
            )

    def ensure(self, height):
        if self.page is None or self.y - height < 46:
            self.new_page()

    def heading(self, title):
        self.ensure(42)
        self.text(self.M, self.y, title, 15, True, NAVY)
        self.y -= 27

    def paragraph(self, value, color=INK, size=10):
        for line in wrap(value, self.W - 2 * self.M, size):
            self.ensure(15)
            self.text(self.M, self.y, line, size, color=color)
            self.y -= 14
        self.y -= 8

    def table(self, headers, rows, widths):
        size, leading = 8.5, 11
        header_lines = [wrap(v, w - 12, size, True) for v, w in zip(headers, widths)]
        header_height = max(map(len, header_lines)) * leading + 12

        def header():
            self.ensure(header_height + 24)
            self.rect(
                self.M, self.y - header_height + 4, sum(widths), header_height, NAVY
            )
            x = self.M
            for lines, w in zip(header_lines, widths):
                for i, line in enumerate(lines):
                    self.text(
                        x + 6, self.y - 8 - i * leading, line, size, True, (1, 1, 1)
                    )
                x += w
            self.y -= header_height

        header()
        for index, row in enumerate(rows):
            lines = [wrap(v, w - 12, size) for v, w in zip(row, widths)]
            length = max(map(len, lines), default=1)
            offset = 0
            while offset < length:
                if self.y - 24 < 46:
                    self.new_page()
                    header()
                available = max(1, int((self.y - 46 - 10) / leading))
                part = min(length - offset, available)
                height = part * leading + 10
                self.rect(
                    self.M,
                    self.y - height + 4,
                    sum(widths),
                    height,
                    (0.95, 0.96, 0.97) if index % 2 == 0 else (1, 1, 1),
                )
                x = self.M
                for value, w in zip(lines, widths):
                    for i, line in enumerate(value[offset : offset + part]):
                        self.text(x + 6, self.y - 7 - i * leading, line, size)
                    x += w
                self.y -= height
                offset += part
                if offset < length:
                    self.new_page()
                    header()
        self.y -= 14

    def finish(self):
        objects = []

        def add(data):
            objects.append(data.encode("cp1252") if isinstance(data, str) else data)
            return len(objects)

        def stream(data, properties=""):
            encoded = zlib.compress(data)
            return add(
                f"<< /Length {len(encoded)} /Filter /FlateDecode {properties} >>\nstream\n".encode()
                + encoded
                + b"\nendstream"
            )

        catalog, pages = add(""), add("")
        regular = add(
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        )
        bold = add(
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
        )
        w, h, rgb, alpha = png_planes(ASSETS / "imperium.png")
        mask = stream(
            alpha,
            f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray /BitsPerComponent 8",
        )
        logo = stream(
            rgb,
            f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /SMask {mask} 0 R",
        )
        children = []
        for i, commands in enumerate(self.pages):
            # Append footer without mutating the reusable drawing commands.
            footer = f"BT /F1 8 Tf 0.45 0.48 0.52 rg 1 0 0 1 38 24 Tm (EVEOSINT | Scientific research | CLASSIFIED) Tj 1 0 0 1 745 24 Tm ({i+1} / {len(self.pages)}) Tj ET"
            content = stream(("\n".join(commands + [footer])).encode("cp1252"))
            children.append(
                add(
                    f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {self.W} {self.H}] /Resources << /Font << /F1 {regular} 0 R /F2 {bold} 0 R >> /XObject << /Logo {logo} 0 R >> >> /Contents {content} 0 R >>"
                )
            )
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages} 0 R >>".encode()
        objects[pages - 1] = (
            f"<< /Type /Pages /Count {len(children)} /Kids [{' '.join(str(c)+' 0 R' for c in children)}] >>".encode()
        )
        metadata = add(
            "<< /Title (First Loss and Observed Return) /Author (EVEOSINT) /Subject (Scientific methodology, results and interpretation) >>"
        )
        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, obj in enumerate(objects, 1):
            offsets.append(len(output))
            output.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
        position = len(output)
        output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
        output.extend("".join(f"{n:010d} 00000 n \n" for n in offsets[1:]).encode())
        output.extend(
            f"trailer\n<< /Size {len(offsets)} /Root {catalog} 0 R /Info {metadata} 0 R >>\nstartxref\n{position}\n%%EOF\n".encode()
        )
        return bytes(output)


def count(value):
    return f"{int(value or 0):,}"


def pct(value):
    return "Unavailable" if value is None else f"{100 * value:.2f}%"


def pp(value):
    return "Unavailable" if value is None else f"{value:+.2f} pp"


def score(value):
    return "Unavailable" if value is None else f"{value:.4f}"


def interval(values, formatter=pct):
    return "Unavailable" if not values else " to ".join(map(formatter, values))


def legacy_variables(report):
    """Recover correlation-circle coordinates from the already archived basis."""
    model = report.get("model") or {}
    axes = model.get("pca_full_axes")
    values = report["summary"].get("pca", {}).get("global", {}).get("eigenvalues")
    names = model.get("feature_names", [])
    if not axes or not values:
        return []
    result = []
    for name, vector in zip(names, axes):
        variance = sum(v * a * a for v, a in zip(values, vector))
        correlations = [
            a * math.sqrt(v / variance) if v > 0 and variance > 1e-12 else None
            for a, v in zip(vector, values)
        ]
        result.append(dict(name=name, correlations=correlations))
    return result


def circle_plot(pdf, variables, first=0, second=1):
    pdf.ensure(325)
    cx, cy, radius = 235, pdf.y - 150, 115
    pdf.circle(cx, cy, radius, GRAY)
    pdf.line(cx - radius, cy, cx + radius, cy)
    pdf.line(cx, cy - radius, cx, cy + radius)
    selected = [
        v
        for v in variables
        if not v["name"].endswith("_missing")
        and len(v["correlations"]) > second
        and v["correlations"][first] is not None
        and v["correlations"][second] is not None
    ]
    selected.sort(
        key=lambda v: v["correlations"][first] ** 2 + v["correlations"][second] ** 2,
        reverse=True,
    )
    for index, v in enumerate(selected[:12], 1):
        x = cx + radius * v["correlations"][first]
        y = cy + radius * v["correlations"][second]
        pdf.line(cx, cy, x, y, (0.12, 0.48, 0.65))
        pdf.circle(x, y, 2.5, (0.12, 0.48, 0.65), True)
        pdf.text(x + 3, y + 4, str(index), 8, True)
        pdf.text(435, cy + 115 - index * 17, f"{index}. {v['name']}", 9)
    pdf.text(cx - 40, cy - radius - 24, "PC1 / PC2", 10, True)
    pdf.y -= 320


def primary_model(summary, horizons, year, scope):
    selected_scope = "pvp" if scope == "all" else scope
    whole = (
        summary.get("return_model", {})
        .get("scopes", {})
        .get(selected_scope, {})
        .get(horizons[-1], {})
    )
    return selected_scope, (
        whole if year == "all" else whole.get("years", {}).get(year, {})
    )


def model_findings(pdf, model, scope, horizon):
    if not model or model.get("unavailable"):
        return
    pdf.paragraph(
        f"Primary comparison: initial {scope} losses and {horizon}-day return. The model is evaluated on {count(model['test_n'])} complete held-out characters. AUC={score(model['auc'])}; Brier={score(model['brier'])} versus year-only baseline {score(model['baseline_brier'])}; relative Brier improvement={pct(model['brier_skill'])}."
    )
    for factor, profiles in [
        ("age", ("7-30 days", "30-365 days", "1-5 years", "5+ years")),
        (
            "ship_role",
            (
                "mining",
                "transport",
                "exploration",
                "logistics",
                "industrial_support",
                "pod",
            ),
        ),
    ]:
        rows = [
            v
            for v in model.get("effects", [])
            if v["factor"] == factor and v["profile"] in profiles
        ]
        for value in rows:
            lo, hi = value["interval_pp"]
            direction = "higher" if lo > 0 else "lower" if hi < 0 else "uncertain"
            pdf.paragraph(
                f"{factor.replace('_',' ')}: {value['profile']} versus {value['reference']} has an adjusted probability difference of {pp(value['effect_pp'])} (approx. 95% interval {interval(value['interval_pp'], pp)}). The direction is {direction}. This comparison keeps the recorded other predictors and loss year fixed.",
                size=9,
            )
    ranking = [
        v
        for v in model.get("importance", [])
        if v.get("importance_percent") is not None
    ][:5]
    if ranking:
        pdf.paragraph(
            "Largest predictive contributions: "
            + "; ".join(
                f"{v['factor'].replace('_',' ')} {v['importance_percent']:.1f}%"
                for v in ranking
            )
            + ". These are shares of positive permutation importance, not percentages of a player's causal decision.",
            size=9,
        )
    pdf.paragraph(
        "Age importance includes birth-date availability and other factor importance includes missingness indicators. An association for an unknown age is a metadata-availability result, not a biological age effect. Known age bands and ship categories should be interpreted separately.",
        size=9,
    )
    for warning in model.get("warnings", []):
        pdf.paragraph(warning, RED)


def character_plot(pdf, summary, scope, horizon, year):
    stat = (
        summary.get("pca_return", {})
        .get("scopes", {})
        .get(scope, {})
        .get(horizon, {})
        .get(year)
    )
    if not stat or len(stat.get("axes", [])) < 2:
        return
    sample = [
        v
        for v in summary["pca_return"].get("sample", [])
        if (year == "all" or v["year"] == year)
        and (
            scope == "all"
            or scope == "pvp"
            and v.get("initial_pvp") is True
            or scope == "npc"
            and v.get("opponent") == "NPC/object"
        )
        and v["outcomes"][horizon]["complete"]
    ]
    pdf.new_page("PCA interpretation: characters and observed return")
    pdf.heading(f"Shared global PCA | {scope} initial loss | {horizon} days | {year}")
    pdf.paragraph(
        f"Colours identify observed return and no observed return. Small points: {count(len(sample))} uniformly sampled display characters. Large markers: means computed from all {count(stat['n'])} complete characters. Outcomes do not determine the fitted PCA axes.",
        size=9,
    )
    axes = stat["axes"][:2]
    sx = max(
        [
            1,
            *[abs(v["coordinates"][0]) for v in sample],
            *[
                abs(v or 0)
                for v in (axes[0]["returned_mean"], axes[0]["no_return_mean"])
            ],
        ]
    )
    sy = max(
        [
            1,
            *[abs(v["coordinates"][1]) for v in sample],
            *[
                abs(v or 0)
                for v in (axes[1]["returned_mean"], axes[1]["no_return_mean"])
            ],
        ]
    )
    cx, cy = 421, 290
    pdf.line(71, cy, 771, cy)
    pdf.line(cx, 110, cx, 470)
    for v in sample:
        pdf.circle(
            cx + 330 * v["coordinates"][0] / sx,
            cy + 165 * v["coordinates"][1] / sy,
            1.5,
            GREEN if v["outcomes"][horizon]["returned"] else RED,
            True,
        )
    for key, color in [("returned_mean", GREEN), ("no_return_mean", RED)]:
        if axes[0][key] is not None and axes[1][key] is not None:
            x = cx + 330 * axes[0][key] / sx
            y = cy + 165 * axes[1][key] / sy
            pdf.circle(x, y, 7, color, True)
            pdf.circle(x, y, 8, INK)
    pdf.text(180, 80, "Green: returned", 10, color=GREEN)
    pdf.text(430, 80, "Red: no observed return", 10, color=RED)
    spectrum = summary.get("pca", {}).get("global", {}).get("spectrum", [])
    pdf.text(
        285,
        57,
        "PC1 "
        + pct(spectrum[0]["variance_ratio"])
        + " | PC2 "
        + pct(spectrum[1]["variance_ratio"]),
        9,
    )
    pdf.y = 48


def make_report(report, horizon="all", year="all", scope="all"):
    s = report["summary"]
    pdf = PDF()
    pdf.new_page(cover=True)
    pdf.rect(0, 0, pdf.W, pdf.H, (0.015, 0.025, 0.04))
    if not s.get("return_model"):
        pdf.text(
            205,
            540,
            "PRELIMINARY SNAPSHOT | Adjusted-return refit required",
            12,
            True,
            (1, 0.45, 0.45),
        )
    pdf.command("q 310 0 0 200 266 302 cm /Logo Do Q")
    pdf.text(280, 263, "CLASSIFIED", 40, True, (0.9, 0.14, 0.18))
    title = "First Loss and Observed Return"
    pdf.text((pdf.W - width(title, 27, True)) / 2, 205, title, 27, True, (1, 1, 1))
    pdf.text(
        229,
        171,
        "Scientific methodology, results and interpretation",
        17,
        color=(0.76, 0.82, 0.9),
    )
    pdf.text(280, 122, "EVEOSINT | THE IMPERIUM", 16, True, (1, 1, 1))
    pdf.text(
        175,
        91,
        f"Run {report['id']} | Data cutoff (exclusive): {str(report['cutoff'])[:10]} | Generated {datetime.now(timezone.utc):%Y-%m-%d} UTC",
        10,
        color=(0.72, 0.77, 0.83),
    )
    pdf.text(
        213,
        66,
        f"Follow-up: {horizon} | Loss cohort: {year} | Initial loss scope: {scope}",
        10,
        color=(0.72, 0.77, 0.83),
    )
    pdf.new_page("Research question and main findings")
    pdf.heading("What predicts recorded return after a first known loss?")
    pdf.paragraph(
        "The primary research question is which initial-loss characteristics are associated with subsequent return, in which direction, by how much, and how these associations change over time. The secondary question is how returning characters change ships, location and combat participation. PCA and clustering describe profiles; they do not replace the analysis of return."
    )
    pdf.paragraph(
        f"This snapshot contains {count(s['players'])} identifiable characters with a recorded loss. NPC losses are included. The first known loss is not necessarily the first actual loss. Return is measured through later published killmail activity, not logins or subscriptions."
    )
    horizons = [str(v) for v in (7, 30, 90)] if horizon == "all" else [horizon]
    cohorts = (
        [("All years", s["overall"]), *sorted(s.get("years", {}).items())]
        if year == "all"
        else [(year, s["years"][year])]
    )
    selected = s["overall"] if year == "all" else s["years"][year]
    pdf.table(
        [
            "Follow-up",
            "Complete cases",
            "Observed return",
            "Return rate",
            "95% Wilson interval",
            "No observed return",
        ],
        [
            [
                h + " days",
                count(selected["horizons"][h]["eligible"]),
                count(selected["horizons"][h]["returned"]),
                pct(selected["horizons"][h]["return_rate"]),
                interval(selected["horizons"][h].get("return_interval")),
                count(
                    selected["horizons"][h]["eligible"]
                    - selected["horizons"][h]["returned"]
                ),
            ]
            for h in horizons
        ],
        [70, 120, 120, 105, 200, 151],
    )
    quality = s.get("data_quality")
    if quality:
        pdf.paragraph(
            f"Data quality: {count(quality['invalid_birthdays'])} placeholder/pre-game birthdays were excluded from age calculations. Their original derived ages are retained for audit. Missing population snapshots prevent treating group size as completely observed.",
            RED,
        )
    else:
        pdf.paragraph(
            "IMPORTANT DATA-QUALITY FINDING: this older snapshot did not exclude Unix-epoch character birthdays. Actual records dated in 1970 produced impossible character ages. Age associations and PCA/cluster interpretation are provisional until the prepared data is refitted with the correction. Return counts do not depend on birthday validity.",
            RED,
        )
    predictive = s.get("return_model")
    if s.get("pca", {}).get("coordinates_persisted") is False:
        pdf.paragraph(
            "This review was recalculated locally from the prepared database rows with a read-only connection. Updated models are included in this report; the production study and its older coordinates were not overwritten. Deploying the update and resuming the Admin job archives the corrected results in the site.",
            GRAY,
            size=9,
        )
    if not predictive:
        pdf.paragraph(
            "Adjusted predictive factor analysis is not present in this stored snapshot. No percentage of a player's decision is fabricated from PCA coefficients. Resume with the updated study to calculate adjusted associations, independent test performance and factor importance.",
            RED,
        )
    else:
        pdf.paragraph(
            "The adjusted return model is assessed on held-out characters. Probability differences are reported in percentage points. Predictive importance is measured by loss of test performance when a factor is shuffled. Both are associations with observed activity, not experimentally identified causal effects."
        )
        main_scope, main = primary_model(s, horizons, year, scope)
        pdf.heading("Main adjusted findings")
        model_findings(pdf, main, main_scope, horizons[-1])
    pdf.heading("Descriptive tendencies in this snapshot")
    h = horizons[-1]
    source = (
        s.get("factors", {}).get(h, {})
        if year == "all"
        else s.get("factors_by_year", {}).get(h, {}).get(year, {})
    )
    effect_key = "year_adjusted_difference_pp" if year == "all" else "difference_pp"
    for family in ("opponent", "ship_role", "corp_kind", "space"):
        for row in source.get(family, []):
            if row["value"] in (
                "NPC/object",
                "players",
                "mining",
                "transport",
                "combat",
                "player",
                "NPC",
                "WH",
                "NS",
            ):
                pdf.paragraph(
                    f"{family} = {row['value']}: {pct(row['rate'])} observed return within {h} days, {pp(row.get(effect_key))} against other profiles in the same loss year (pooled across supported years when applicable); n={count(row['n'])}.",
                    size=9,
                )
    pdf.new_page("Scientific methodology")
    pdf.heading("Cohort, outcomes and time")
    for paragraph in (
        "All identifiable characters with at least one recorded loss enter the study. Characters without a loss and anonymous unmatched MER losses are excluded. Initial losses against players and against NPCs/objects are separate adjusted-model populations; an all-loss model is retained for comparison.",
        "Events in the first 30 minutes are part of the initial episode. Return means later recorded attacker or victim activity. PvP return requires a player victim and a player attacker. Each 7-, 30- and 90-day rate excludes characters with incomplete imported-day coverage or insufficient elapsed follow-up. A known return in an incomplete window is retained separately, not mixed into complete-window rates.",
        "Predictors describe the initial loss or earlier history: age, corporation tenure, ship role, security zone, attackers, item rows, earlier activity and the prior 30-day corporation/alliance context. Current skill guesses, future corporation membership and future activity are not predictors. Item rows do not establish item value or fitting quality. Corporation attack counts are participations, not unique active members.",
        "Loss year is controlled explicitly. Numeric predictors are log-transformed where appropriate, normalized within year and bounded. Birthdays before 2000 are treated as unknown instead of character age. Missing data has explicit indicators; unavailable snapshots are not substituted with current populations.",
    ):
        pdf.paragraph(paragraph)
    pdf.heading("Adjusted return model and independent assessment")
    for paragraph in (
        "The supervised model is a multivariable logistic regression with loss-year controls and a fixed ridge penalty (0.001 on mean training loss). Age is represented by age bands; ship role is categorical. References are stated in every comparison. Streaming iteratively reweighted least squares fits complete training cases without keeping all player records in RAM. It is separate from outcome-free PCA and clustering.",
        "A deterministic hash of character ID creates an approximately 80% training / 20% evaluation split. The same character stays in the same side for all horizons. Preprocessing statistics and fitted coefficients use training characters only. All eligible held-out characters are evaluated; this is not a fit on a few hundred players. Random within-era validation does not demonstrate performance in a future era.",
        "Adjusted differences average model probabilities after replacing a categorical profile with its stated reference, keeping other recorded predictors and year fixed. Numeric contrasts increase a known transformed predictor by one training standard deviation, bounded at the preprocessing limit. Approximate 95% intervals use a training-only sandwich covariance and the delta method. They assume independent characters and do not account fully for shared battles or corporations. Estimates are exploratory and can extrapolate beyond well-supported profile combinations.",
        "Brier score measures mean squared probability error; lower is better. The baseline uses training return rates for the corresponding loss year. Brier skill measures improvement over this baseline. AUC measures probability ordering; it is approximated with 1,024 probability bins. Grouped permutations shuffle a factor and its missingness/category columns together across the entire held-out population of each loss year, with three repeats. They are not restricted to neighbouring character-ID batches. Positive increases in held-out Brier error determine relative predictive importance. Correlated factors can share information or produce unrealistic shuffled combinations; these shares are not a unique causal allocation of a player's decision.",
        "Normalized importance is withheld when the model fails to converge or does not beat the year-only baseline. A small or uncertain estimate means little evidence under this model and data, not proof that a factor has no possible effect. Many comparisons are exploratory; narrow intervals do not remove publication or metadata bias.",
    ):
        pdf.paragraph(paragraph)
    pdf.heading("PCA, supplementary outcomes and clusters")
    for paragraph in (
        "PCA (ACP) transforms correlated input variables into orthogonal components. Each component is a combination of variables. Its percentage is its share of total profile variance. It is not the percentage of the return decision explained. The correlation circle identifies which variables characterize an axis, as in environmental PCA of river stations.",
        "All 52 coordinates are preserved per character. Enough components to retain at least 90% of variance are used for profile clustering. Yearly PCAs are diagnostic, independently oriented bases. Return is a supplementary outcome: character positions are distinguished by observed return, and axis-return correlations and group means use every complete case. Display points use only a bounded uniform sample.",
        "Auto grouping compares 2-30 counts and three starts, using separation (silhouette) and repeatability (adjusted Rand index). It then fits the selected count on all characters. A stable grouping with weak separation does not establish distinct natural player types or validate a prediction of return. The adjusted return model uses original predictors, not clusters or PCA coordinates.",
    ):
        pdf.paragraph(paragraph)
    if predictive:
        pdf.new_page("Primary results: adjusted factors and prediction")
        pdf.heading("Does the model predict return beyond the loss-year baseline?")
        scopes = ["pvp", "npc", "all"] if scope == "all" else [scope]
        metrics = []
        for name in scopes:
            for h in horizons:
                whole = predictive.get("scopes", {}).get(name, {}).get(h, {})
                model = whole if year == "all" else whole.get("years", {}).get(year, {})
                if model.get("unavailable") or not model:
                    continue
                metrics.append(
                    [
                        name,
                        h,
                        count(model["training_n"]),
                        count(model["test_n"]),
                        score(model["brier"]),
                        score(model["baseline_brier"]),
                        pct(model["brier_skill"]),
                        score(model["auc"]),
                    ]
                )
        pdf.table(
            [
                "Initial loss",
                "Days",
                "Training",
                "Held-out",
                "Brier",
                "Year baseline",
                "Brier skill",
                "AUC",
            ],
            metrics,
            [85, 40, 105, 105, 100, 120, 110, 101],
        )
        for name in scopes:
            for h in horizons:
                whole = predictive.get("scopes", {}).get(name, {}).get(h, {})
                model = whole if year == "all" else whole.get("years", {}).get(year, {})
                pdf.heading(f"Initial loss: {name} | {h}-day return | cohort {year}")
                if not model or model.get("unavailable"):
                    pdf.paragraph(model.get("unavailable", "Analysis unavailable."))
                    continue
                for message in model.get("warnings", []):
                    pdf.paragraph(message, RED)
                pdf.table(
                    [
                        "Factor",
                        "Profile / change",
                        "Reference",
                        "Adjusted difference",
                        "Approx. 95% interval",
                    ],
                    [
                        [
                            v["factor"],
                            v["profile"],
                            v["reference"],
                            pp(v["effect_pp"]),
                            interval(v.get("interval_pp"), pp),
                        ]
                        for v in model.get("effects", [])
                    ],
                    [150, 220, 155, 110, 131],
                )
                pdf.table(
                    [
                        "Factor",
                        "Predictive importance",
                        "Test Brier increase",
                        "Approx. 95% interval",
                    ],
                    [
                        [
                            v["factor"],
                            (
                                "Not established"
                                if v["importance_percent"] is None
                                else f"{v['importance_percent']:.2f}%"
                            ),
                            f"{v['delta_brier']:.6f}",
                            " to ".join(f"{x:.6f}" for x in v["interval"]),
                        ]
                        for v in model.get("importance", [])
                    ],
                    [250, 160, 160, 196],
                )
    pdf.new_page("Raw descriptive results: complete follow-up")
    pdf.paragraph(
        "The following descriptive counts include all initial-loss types. They are separate from the selected initial-loss population in the adjusted model. Wilson intervals quantify binomial sampling uncertainty and do not measure publication bias."
    )
    for h in horizons:
        pdf.heading(f"Return by first-loss year | {h} days")
        pdf.table(
            [
                "Cohort",
                "Characters",
                "Complete",
                "Returned",
                "No return",
                "Return rate / 95% interval",
                "PvP return",
                "Return without PvP",
            ],
            [
                [
                    label,
                    count(b["n"]),
                    count(b["horizons"][h]["eligible"]),
                    count(b["horizons"][h]["returned"]),
                    count(b["horizons"][h]["eligible"] - b["horizons"][h]["returned"]),
                    pct(b["horizons"][h]["return_rate"])
                    + " / "
                    + interval(b["horizons"][h].get("return_interval")),
                    count(b["horizons"][h]["pvp"]),
                    count(b["horizons"][h]["returned_without_pvp"]),
                ]
                for label, b in cohorts
            ],
            [70, 80, 80, 80, 80, 205, 80, 91],
        )
        pdf.heading(f"Single-factor comparisons | {h} days")
        source = (
            s.get("factors", {}).get(h, {})
            if year == "all"
            else s.get("factors_by_year", {}).get(h, {}).get(year, {})
        )
        pdf.table(
            [
                "Factor",
                "Profile",
                "Supported cases",
                "Return rate",
                "95% interval",
                "Difference vs others",
            ],
            [
                [
                    name,
                    v["value"],
                    count(v["n"]),
                    pct(v["rate"]),
                    interval(v.get("interval")),
                    pp(v.get(effect_key)),
                ]
                for name, values in sorted(source.items())
                for v in values
            ],
            [170, 190, 100, 85, 110, 111],
        )
        pdf.paragraph(
            "Differences above compare other profiles within the same loss year. Pooled differences weight yearly differences by supported profile count. At least 20 cases on both sides of a supported year are required. These comparisons do not adjust for every other factor simultaneously.",
            size=9,
        )
        pdf.heading(f"What changed after return? | {h} days")
        evolution = selected["horizons"][h].get("evolution", {})
        rows = []
        for path, values in evolution.items():
            for measure in ("zone", "system", "region", "ship", "ship_role"):
                known = values.get(measure + "_known", 0)
                same = values.get("same_" + measure, 0)
                rows.append(
                    [
                        path,
                        measure,
                        count(values["returned"]),
                        count(same),
                        count(known - same),
                        count(values["returned"] - known),
                        pct(same / known) if known else "Unavailable",
                    ]
                )
        pdf.table(
            [
                "Return path",
                "Compared feature",
                "Returned",
                "Same",
                "Different",
                "Unknown",
                "Same / known",
            ],
            rows,
            [170, 145, 95, 85, 85, 85, 101],
        )
        for path, values in evolution.items():
            if h != "7":
                pdf.paragraph(
                    f"{path}: further loss in window = {count(values.get('repeat_losses'))}; subsequent attacker activity = {count(values.get('attacks'))}. Attacking again does not establish revenge. Many characters are observed returning through further losses, so repeat-loss proportions reflect this detection process.",
                    size=9,
                )
    pdf.new_page("PCA: complete axis results and interpretation")
    pca = s.get("pca", {}).get("global", {})
    pdf.heading("Global PCA: all stored components")
    if pca:
        pdf.paragraph(
            f"{count(pca.get('dimensions'))} axes; numerical rank {count(pca.get('rank'))}. {count(pca.get('components_retained'))} axes retain {pct(pca.get('retained_variance_ratio'))} of profile variance. Outcome information is excluded when fitting these axes."
        )
        pdf.table(
            [
                "Axis",
                "Variance",
                "Cumulative",
                "Used for 90%",
                "Largest input coefficients",
            ],
            [
                [
                    "PC" + str(v["component"]),
                    pct(v.get("variance_ratio")),
                    pct(v.get("cumulative_ratio")),
                    "Yes" if v.get("retained") else "No",
                    "; ".join(
                        f"{f['name']}: {f['coefficient']:+.3f}"
                        for f in v.get("top_features", [])
                    )
                    or "No measurable variation",
                ]
                for v in pca.get("spectrum", [])
            ],
            [50, 75, 85, 85, 471],
        )
        pdf.heading("Correlation circle: PC1 and PC2")
        circle_plot(pdf, pca.get("variables") or legacy_variables(report))
        pdf.paragraph(
            "Vector endpoints are variable-axis correlations, not percentages of a player's decision. Nearby directions identify correlated variables on this plane. Short vectors may be poorly represented on these two axes. All components, including axes beyond this figure, are reported above.",
            size=9,
        )
    else:
        pdf.paragraph("PCA diagnostics are unavailable in this legacy snapshot.")
    character_plot(pdf, s, "pvp" if scope == "all" else scope, horizons[-1], year)
    for name in (["pvp", "npc", "all"] if scope == "all" else [scope]):
        for h in horizons:
            comparison = (
                s.get("pca_return", {})
                .get("scopes", {})
                .get(name, {})
                .get(h, {})
                .get(year)
            )
            if not comparison:
                continue
            pdf.heading(f"PCA linked to return | {name} | {h} days | {year}")
            pdf.paragraph(
                f"Complete characters: {count(comparison['n'])}; returned: {count(comparison['returned'])}. Means and correlations use all complete cases in the shared global PCA basis. They are supplementary outcome statistics, not cluster results."
            )
            pdf.table(
                ["Axis", "Correlation with return", "Returned mean", "No-return mean"],
                [
                    [
                        "PC" + str(v["component"]),
                        score(v.get("correlation")),
                        score(v.get("returned_mean")),
                        score(v.get("no_return_mean")),
                    ]
                    for v in comparison["axes"]
                ],
                [80, 230, 228, 228],
            )
    pdf.heading("Yearly PCA overview")
    yearly = s.get("pca", {}).get("years", {})
    pdf.table(
        [
            "Year",
            "Characters",
            "PC1",
            "PC2",
            "PC3",
            "Axes for 90%",
            "Retained variance",
        ],
        [
            [
                y,
                count(p.get("players")),
                *[
                    (
                        pct(p.get("spectrum", [])[i].get("variance_ratio"))
                        if len(p.get("spectrum", [])) > i
                        else "Unavailable"
                    )
                    for i in range(3)
                ],
                count(p.get("components_retained")),
                pct(p.get("retained_variance_ratio")),
            ]
            for y, p in sorted(yearly.items())
            if year == "all" or y == year
        ],
        [75, 115, 110, 110, 110, 130, 116],
    )
    pdf.paragraph(
        "Yearly axes are independently fitted. PC1 in one year is not necessarily the same direction as PC1 in another. Comparing percentages alone cannot establish that the same behavioural factor changed its influence on return.",
        size=9,
    )
    pdf.heading("Initial input dimensions")
    names = (report.get("model") or {}).get("feature_names", [])
    if names:
        pdf.table(
            ["Dimension", "Input variable / encoding"],
            [[str(i), name] for i, name in enumerate(names, 1)],
            [90, 676],
        )
    pdf.new_page("Analysis and conclusions")
    pdf.heading("What the evidence supports")
    if predictive:
        main_scope, main = primary_model(s, horizons, year, scope)
        model_findings(pdf, main, main_scope, horizons[-1])
    if quality:
        pdf.heading("Availability of initial-loss measurements")
        pdf.table(
            ["Variable", "Missing / unavailable", "Available"],
            [
                [name, count(missing), pct(1 - missing / s["players"])]
                for name, missing in quality.get("missing_features", {}).items()
            ],
            [350, 200, 216],
        )
    pdf.paragraph(
        "The descriptive evidence can identify loss categories with more or less subsequently recorded activity. It can also describe returning characters who change ships or zones, suffer further losses or become attackers. Such differences address the research question directly, while remaining conditional on killmail observation."
    )
    pdf.paragraph(
        "A completed adjusted model adds comparisons of age and ship profiles after controlling the other recorded variables and loss year. Independent test performance determines whether the joint model predicts return beyond a year-only reference. Direction, effect size, uncertainty and predictive importance answer different parts of the question and must be read together."
    )
    selection = s.get("cluster_selection", {})
    pdf.paragraph(
        f"Secondary profile grouping selected {count(selection.get('selected_k'))} groups. Final silhouette: {score(selection.get('final_silhouette'))}; population ARI: {score(selection.get('population_stability_ari'))}. Stable assignments can coexist with weak separation. These diagnostics describe reproducibility of groups, not the percentage of return explained."
    )
    for warning in selection.get("warnings", []):
        pdf.paragraph(warning, color=RED, size=9)
    pdf.heading("Limits and next scientific checks")
    for paragraph in (
        "No observed killmail return is not synonymous with leaving EVE. A character may continue mining, trading, running missions, changing accounts or avoiding published losses. CCP login/subscription or consenting-player activity data would be needed to identify actual game retention.",
        "The first observed loss can be historically left-censored. Imported-day completeness does not remove unpublished killmails. Present-day SDE geography and sparse historical populations limit historical environmental inference. First-loss ship class suggests activity but does not prove intent.",
        "Character-level validation and approximate intervals do not fully address dependencies within battles, corporations or accounts. Era holdouts, corporation/battle-blocked validation, sensitivity to observation windows, publication coverage and missing metadata should test robustness before policy or causal claims. Nonlinear models or interactions may find patterns the additive logistic model misses.",
        "There is no unique model-independent percentage of a player's decision attributable to age or ship. This report provides measurable category return rates, adjusted probability contrasts and validated predictive contributions. Causal influence requires a defensible additional design, such as an intervention or an identifiable natural experiment.",
    ):
        pdf.paragraph(paragraph)
    pdf.heading("Conclusion")
    pdf.paragraph(
        "The research result is the comparison of recorded return outcomes across loss characteristics and eras, supported by a multivariable model and independent assessment. PCA helps interpret how those profiles are arranged; clusters summarize them. Unsupported or missing measurements remain unavailable rather than being converted into apparent certainty."
    )
    pdf.heading("Sources and reproducibility")
    pdf.paragraph(
        f"Source: EVEOSINT stored research run {report['id']}, completed snapshot, cutoff {report['cutoff']}. Configuration, preprocessing, fitted coefficients, covariance, PCA bases and all per-character coordinates are preserved by the study. Exporting this report does not launch a job or scan the raw killmail tables.",
        size=9,
    )
    pdf.paragraph(
        "PCA method and variable/individual coordinates: https://adeverse.github.io/ade4/reference/dudi.pca.html ; PCA implementation reference: https://scikit-learn.org/stable/modules/generated/sklearn.decomposition.PCA.html ; prediction assessment: https://scikit-learn.org/stable/modules/permutation_importance.html . Cover artwork supplied by the user.",
        size=8,
    )
    return pdf.finish()
