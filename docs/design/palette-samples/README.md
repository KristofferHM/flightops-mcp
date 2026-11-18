# Palette samples for the airport board

Four versions of the same screen, for choosing the board's visual identity from pictures. Open any HTML file
directly. Each one is a single file with its data, coastline and libraries inside, and makes no network requests.
These are design samples: the buttons and controls do nothing, and the shipping board is unchanged.

## The four palettes

**A, Aeronautical chart (`a-chart.html`).** Paper, pale water, hairline coasts, a barred neatline and magenta
selection, borrowed from printed flight charts. It feels like a working document that someone trained would trust.
It does one thing better than the others: the globe reads as a real map, with land, water and coast separate at a
glance. Its weakness is the state colours. For a deuteranope, VFR green and IFR red are almost the same colour, and
MVFR amber falls below 3:1 on land and water, so outlines and shapes do work the colours cannot.

**B, Radar scope (`b-radar.html`).** A dark scope with a bearing ring and range rings measured from the selected
airport. It feels alert and nocturnal, like a control room. It does one thing better than the others: state markers
stand out most, with at least 6:1 against every map surface. Its weakness is what it implies. A radar look suggests
live surveillance, while this board often shows an old snapshot, and all-monospace text truncates airport names the
most.

**C, Scandinavian timetable (`c-timetable.html`).** White space, hairline rules, large numbers and one blue for
anything interactive. It feels calm and finished, more product than instrument. It does one thing better than the
others: the panels are the easiest to read, including for someone who does not work in aviation. Its weakness is the
globe, which recedes. Land had to go very pale to keep MVFR at 3:1, and this is the least distinctive of the four.

**D, Blueprint (`d-blueprint.html`).** White linework on deep blue, with a faint grid, centre lines and a dimension
line. It feels precise and a little ceremonial. It does one thing better than the others: it is the only palette that
passes every check below, because its state colours were derived and could be spaced apart in lightness. Its weakness
is that VFR, the most common state, is the dimmest marker, and spaced capitals over a grid make the panels the
busiest to read. It also looks like a plan of something rather than a report of what is happening.

## Checks

| | A | B | C | D |
|---|---|---|---|---|
| Lowest text contrast on the page (needs 4.5:1) | 5.9:1 | 6.7:1 | 4.85:1 | 4.8:1 |
| Weakest state colour against a map surface (target 3:1) | **MVFR 2.6:1 on water, 2.9:1 on land** | IFR 6.0:1 | MVFR 3.0:1 | VFR 3.2:1 |
| Closest state pair for a deuteranope (target ΔE 15) | **VFR/IFR 3.7** | **VFR/IFR 13.5** | **VFR/IFR 13.0** | MVFR/IFR 16.2 |
| Weakest state colour against a map surface for a deuteranope | ΔE 31.2 | ΔE 51.7 | ΔE 36.3 | ΔE 38.5 |

Bold marks a failure. Colours in A, C and B's VFR and IFR are the brief's, so they were not changed to pass. The brief
asks for a shape or label instead, and every failure is covered the same way:

- Each category has its own shape: circle, square, diamond, triangle. Marker size also grows with severity.
- The category is written in words in the list, the legend and the detail panel.
- Text never takes its colour from the category it describes.
- In A, every marker has a dark ink outline, which keeps MVFR's shape visible on land and water.

`Unknown` is never filled and uses the text colour, not a severity colour: a hatched ring in A, dashed rings in B and
C, and a dotted ring in D.

Text contrast was measured from the rendered pages at 1440 px and 390 px. Deuteranopia uses the Machado 2009
simulation at full severity, compared with CIEDE2000. The ΔE 15 target is a working threshold chosen for small map
markers, not a published standard.

## What differs from the brief

- **Aircraft heard says 4, not 5.** The brief's example says 5, and `data.json` says 4. The data wins.
- **The globe is centred on 42°N 10°E and cropped at the south.** A whole-sphere fit packed the European airports
  into a small area. In d3 the centre is passed negated, as `rotate([-10, -42])`, so the brief's example `[-10, 55]`
  would have faced the southern hemisphere.
- **The list shows 12 of the 27 airports,** so every category and both sample fixtures are visible together.
- **B's amber is used for selection only,** because the brief makes IFR the only warm state colour. MVFR blue and LIFR
  pale violet are derived.
- **D's state colours and C's land tone are derived,** keeping A's hues for D and adjusting lightness to pass.
- **The coastline needs `topojson-client`,** the same vendored decoder the board uses, in addition to the listed files.
- **Fonts fall back on machines without them.** Inter, IBM Plex and JetBrains Mono are named first but cannot be
  downloaded. Without them the samples use Segoe UI and Cascadia Mono or Consolas.

`XXXX` and `YYYY` are sample fixtures, labelled on the map and in the legend. The list says 53 min and the detail
panel 54 min for ESGG because `data.json` holds both readings.
