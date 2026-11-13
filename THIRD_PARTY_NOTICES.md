# Third-party software

Smart Roots LIMS is AGPL-3.0-only (copyright and trademark notice: [NOTICE](NOTICE)). Third-party packages retain their own licenses and notices.
The root LICENSE is the unmodified text retrieved from <https://www.gnu.org/licenses/agpl-3.0.txt>.
`uv.lock` identifies the exact Python distributions; installed distributions retain their license
files in the image. Do not strip those files or the notices in bundled browser assets.

| Component | Upstream license / source |
| --- | --- |
| Django | BSD-3-Clause; https://github.com/django/django/blob/main/LICENSE |
| django-allauth | MIT; https://github.com/pennersr/django-allauth/blob/main/LICENSE |
| fido2 (via django-allauth[mfa]) | BSD-2-Clause OR Apache-2.0 OR MPL-2.0 (Yubico); retain COPYING, COPYING.APLv2, COPYING.MPLv2 |
| qrcode (via django-allauth[mfa]) | BSD-3-Clause (Lincoln Loop), including code under the MIT license (Kazuhiko Arase); "QR Code" is a registered trademark of DENSO WAVE INCORPORATED |
| cryptography | Apache-2.0 OR BSD-3-Clause; retain LICENSE, LICENSE.APACHE, LICENSE.BSD (wheels bundle OpenSSL, Apache-2.0) |
| Psycopg / psycopg-binary | LGPL-3.0-only; https://github.com/psycopg/psycopg/blob/master/LICENSE.txt |
| Gunicorn | MIT; https://github.com/benoitc/gunicorn/blob/master/LICENSE |
| WhiteNoise | MIT; https://github.com/evansd/whitenoise/blob/main/LICENSE |
| WeasyPrint | BSD-3-Clause; https://github.com/Kozea/WeasyPrint/blob/main/LICENSE |
| Pyphen (with its dictionaries) | GPL-2.0-or-later OR LGPL-2.1-or-later OR MPL-1.1; retain COPYING.GPL/COPYING.LGPL/COPYING.MPL and the per-dictionary README_hyph_*.txt files |
| Pillow | MIT-CMU; wheels bundle third-party libraries whose notices are in the installed LICENSE file |
| python-barcode | MIT; https://github.com/WhyNotHugo/python-barcode/blob/v0.16.1/LICENCE |
| SciPy / NumPy | BSD-3-Clause; https://github.com/scipy/scipy/blob/v1.18.1/LICENSE.txt and https://github.com/numpy/numpy/blob/v2.5.4/LICENSE.txt; retain bundled numerical-library notices |
| dj-database-url | BSD-3-Clause; https://github.com/jazzband/dj-database-url/blob/master/LICENSE |
| Bootstrap 5.3.8 | MIT; https://github.com/twbs/bootstrap/blob/v5.3.8/LICENSE; retain bundled license header/file under static/vendor/bootstrap-5.3.8/ |
| Bootstrap Icons 1.13.2 (subset in static/img/icons.svg, symbols copied verbatim) | MIT; https://github.com/twbs/icons/blob/v1.13.2/LICENSE; retain static/img/icons.LICENSE |
| Overpass (variable, latin and latin-ext subsets, via @fontsource-variable/overpass 5.3.0) | SIL Open Font License 1.1; Copyright 2021 The Overpass Project Authors (https://github.com/RedHatOfficial/Overpass); retain static/vendor/overpass-5.3.0/OFL.txt; the subsets are also embedded in issued report PDFs |
| HTMX 2.0.11 | 0BSD; https://github.com/bigskysoftware/htmx/blob/v2.0.11/LICENSE; retain bundled LICENSE under static/vendor/htmx-2.0.11/ |
| Tom Select 2.7.0 (including bundled Sifter and microplugin) | Apache-2.0; https://github.com/orchidjs/tom-select/blob/v2.7.0/LICENSE; retain bundled license and headers under static/vendor/tom-select/ |
| Vanilla Calendar Pro 3.4.0 | MIT; https://github.com/uvarov-frontend/vanilla-calendar-pro/blob/v3.4.0/LICENSE; retain bundled LICENSE and the header under static/vendor/vanilla-calendar-pro-3.4.0/ |
| Chart.js 4.6.1 / bundled @kurkle/color 0.3.2 | MIT; https://github.com/chartjs/Chart.js/blob/v4.6.1/LICENSE.md; retain both license files under static/vendor/chartjs-4.6.1/ |
| DataTables 3.1.5 / ColReorder 3.1.2 (with Bootstrap 5 integrations) | MIT; https://datatables.net/license/mit; retain bundled License.txt under static/vendor/datatables/ |
| Playwright / pytest-playwright (optional test tools) | Apache-2.0; https://github.com/microsoft/playwright-python/blob/main/LICENSE |

Using the [GNU license compatibility list](https://www.gnu.org/licenses/license-list.html),
review of the resolved Python distribution metadata and license files (2026-11-11) found
permissive BSD/MIT/Apache alternatives and LGPL/GPL licensing choices compatible with AGPL-3.0. Pyphen's code
is GPL-2.0-or-later/LGPL-2.1-or-later/MPL-1.1 tri-licensed; the GPL-2.0-or-later option, used as GPLv3, applies here.
Its bundled dictionaries carry individual notices (mostly GPL/LGPL/MPL; the Icelandic one is
CC BY 4.0 and the Belarusian one is CC BY-SA 4.0 or LGPLv3) that must also be retained. FIDO2 is
offered under BSD-2-Clause, Apache-2.0 or MPL-2.0 and the QR code package is BSD-3-Clause with an
embedded MIT notice; their package metadata also lists extra classifiers (including
"Other/Proprietary" for qrcode), but the shipped COPYING/LICENSE files are the source of terms.

The container image is built on `python:3.14.10-slim-bookworm` (Debian 12) and adds Debian's
Pango, HarfBuzz and DejaVu font packages and their dependencies, plus the `uv` binary from
`ghcr.io/astral-sh/uv`. These keep the licences and notices recorded under
`/usr/share/doc/<package>/copyright`; Debian publishes the corresponding source for its packages.
Python wheels for Linux bundle native libraries whose notices ship in each package's metadata.
A redistributed binary/container release must include applicable corresponding
source and relinking materials required by its copyleft dependencies. Recheck changed packages,
native libraries and bundled assets before distribution; this review covers the source tree and
resolved Python distributions, not the contents of a built image.
