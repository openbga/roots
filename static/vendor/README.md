# Vendored frontend assets

Pinned release files, copied unmodified from the npm registry tarballs. No build step and no
runtime CDN. To upgrade, download the new tarball, check its `dist.integrity` against the
registry, replace the directory, update this table and
[Third-party notices](../../THIRD_PARTY_NOTICES.md), and update the paths in the templates that
load the files (`templates/layouts/document.html`, `templates/500.html` and the chart page in
`analytical/templates/`). Collect static assets afterwards.

| Package | Version | Files | License | Tarball integrity (sha512) |
| --- | --- | --- | --- | --- |
| bootstrap | 5.3.8 | `dist/css/bootstrap.min.css`, `dist/js/bootstrap.bundle.min.js` (+ maps) | MIT | `HP1SZDqaLDPwsNiqRqi5NcP0SSXciX2s9E+RyqJIIqGo+vJeN5AJVM98CXmW/Wux0nQ5L7jeWUdplCEf0Ee+tg==` |
| htmx.org | 2.0.11 | `dist/htmx.min.js` | 0BSD | `Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==` |
| tom-select | 2.7.0 | `dist/js/tom-select.complete.js` and `.min.js` (+ maps; the page loads the minified one), `dist/css/tom-select.bootstrap5.css` (+ map) | Apache-2.0 | `i/iuTfrzYfGPAILfgPdU9/CCSm8XcnomjO2bHjAcFQPV7od0ghVngjaMfB5x5Agkrpe+49/tK6soegrwWyi6Lg==` |
| vanilla-calendar-pro | 3.4.0 | `index.js` (browser-global build: `window.VanillaCalendarPro`), `styles/layout.css` kept as `layout.css` (layout only; app.css colours it), `LICENSE` | MIT | `HLWjzHOWvMIrjI77tXK5bYzp0s5TeBSrZC247STjcTAdWI7kqEwWT6Xvu20vqdwzF498H1Da8l4WpdgVIGtc1Q==` |
| chart.js | 4.6.1 | `dist/chart.umd.min.js` (+ map), `LICENSE.md` | MIT | `hJyniucsFmq//EvXYvJ6c6mahPoI3kaxdjVH8awTaJ/fDNWeSqqqArixMJWkWNN6fcYvPU3HneSANFNYtRi61g==` |
| @kurkle/color (bundled in chart.js) | 0.3.2 | `LICENSE.md`, kept as `KURKLE-COLOR-LICENSE.md` | MIT | `fuscdXJ9G1qb7W8VdHi+IwRqij3lBkosAm4ydQtEmbY58OzHXqQhvlxqEkoz0yssNVn38bcpRWgA9PP+OGoisw==` |
| datatables.net | 3.1.5 | `dataTables.min.js`, `License.txt` | MIT | `phu9fVz1cOhD7kZDO5Ztj15qwghCkmdwX5AezLi+PxfMtnFAq0itpewvXzdwdr6bOwPqO2zuXsXWUU5x3wQptw==` |
| datatables.net-bs5 | 3.1.5 | `dataTables.bootstrap5.min.js`, `dataTables.bootstrap5.min.css` | MIT | `eGSe7DVcqqI0RKtrjgzFVqqCZoltJSahAKLCJ0nH0WzSfVclhD8maoGbbz/JJQXky1H9c+vzAnbmAevYRBjxOw==` |
| datatables.net-colreorder | 3.1.2 | `dataTables.colReorder.min.js` | MIT | `+PckBw8ayHeOXk1qEWMZLzEAHXu4nSVGb5ef7PTFqwYQrPLsZ0xa6A9boUu1se+4xQj/4vPtZ+0Hj3HiIunA3A==` |
| datatables.net-colreorder-bs5 | 3.1.2 | `colReorder.bootstrap5.min.css` | MIT | `4uBj32P0wHDsCW8+9Goy8fD7HPDUQNANeJqU+2nTkVESDrgU7I4R7PBHi+W9TG84cBixpUptPg8wtzL4t3I0EQ==` |
| @fontsource-variable/overpass | 5.3.0 (Overpass v19, variable weight 100–900) | `files/overpass-latin-wght-normal.woff2`, `files/overpass-latin-ext-wght-normal.woff2`, `LICENSE` kept as `OFL.txt`; loaded by `@font-face` in `static/css/app.css` and, for issued report PDFs, in `reporting/static/reporting/report.css` (served to WeasyPrint by `reporting/pdf.py`) | OFL-1.1 | `HviVvVvKCwhXjxM61GjnCW2cKoeEWFkoBANaArzIHliC+7mV4lmKKIBAa0O7E5ISHnkV3sihgOHXxzpnsbP3Xg==` |

`static/img/icons.svg` is a subset of Bootstrap Icons 1.13.2 (MIT); its licence is
`static/img/icons.LICENSE`. Each `<symbol id="i-NAME">` holds the inner markup of
`icons/NAME.svg` from the `bootstrap-icons@1.13.2` npm tarball, copied verbatim; add a symbol the
same way.
