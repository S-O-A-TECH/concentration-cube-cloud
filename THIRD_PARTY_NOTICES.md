# Third-party notices

The MIT license in [`LICENSE`](LICENSE) covers this project's own source code.
Bundled third-party assets are listed here and remain under their own licenses.

## MaruBuri (마루 부리)

- Files: `live-evolution-server/local/app/webui/fonts/MaruBuri-Regular.ttf`, `MaruBuri-Bold.ttf`
- Author: NAVER Corp.
- Source: https://hangeul.naver.com/font
- License: SIL Open Font License 1.1 — https://scripts.sil.org/OFL

Used as the heading typeface of the evolution server's web console
(`app/webui/css/app.css`). Redistributed unmodified. The OFL permits
redistribution provided the font is not sold on its own and this notice is
retained; it does **not** extend to the rest of this repository.

If you prefer not to redistribute the font, delete the two `.ttf` files and the
two `@font-face` rules at the top of `app/webui/css/app.css` — headings fall
back to the generic `serif` family and the console remains fully functional.

## Python dependencies

Runtime dependencies are declared in `server/local/requirements.txt` and
`live-evolution-server/local/requirements.txt`. They are installed from PyPI at
build time and are not vendored into this repository; each remains under its own
license.
