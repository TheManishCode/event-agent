# Third-party assets

Two files in `eventmate/web/static/` are not ours. Both are served only by the allow-list in `eventmate/web/app.py`, and both are loaded lazily, so a failure to load either leaves the page working without the avatar.

## Assistant avatar: `avatar.glb`

- Title: Polytoria Avatar V3 "Pauly"
- Author: dantheman (https://sketchfab.com/beachr267)
- Source: https://sketchfab.com/3d-models/polytoria-avatar-v3-pauly-2c1fbaf2dec1411ca1ae5bfd0afaaa9e
- Licence: Creative Commons Attribution 4.0 (https://creativecommons.org/licenses/by/4.0/). Attribution is required and is shown in the page footer.
- Changes: none. The file is byte-for-byte what was downloaded (it carries its own licence and author in its metadata).
- SHA-256: `cf98b66b20b9480db1e981eb65c56861a88da547af91784919c6263100729673`
- Why this model: 880 KB and about 3,500 triangles, and it ships 23 animations (Idle, Wave, Point, Dance and others) so the avatar can react without any extra tooling. A second candidate (a commercial brand mascot) was not used: it had no animations, was larger, and is a trademarked character.

## 3D viewer: `model-viewer.min.js`

- Project: `<model-viewer>` by Google (https://modelviewer.dev, https://github.com/google/model-viewer), npm package `@google/model-viewer` 4.4.0
- Licence: Apache-2.0 (the licence text is in the file header; it bundles three.js, MIT, and fflate, MIT)
- Changes: none (the published `dist/model-viewer.min.js`)
- SHA-256: `18b2e711035aa4705ee281530a55dc2338ff05eed3d70ae16d54105282a75ed5`
- It mentions Google-hosted decoders for compressed models. This avatar uses none, and the page's Content-Security-Policy (`connect-src 'self'`) would block them anyway.

## Content-Security-Policy impact

Found by running it in a real browser against the strict policy, then loosened as narrowly as possible:

- `style-src` gained one exact hash (`sha256-PXtZZkJMMyc18o/DN1ZEFOJ2qEGq95IQbVaJFMOXOb0=`), the one fixed `<style>` block the viewer injects. It is a hash, not `unsafe-inline`, so any other inline style is still blocked. A viewer upgrade may change the block and its hash; the browser test fails loudly if so.
- `img-src` and `connect-src` gained `blob:`: the viewer decodes the model's textures from the model file and fetches them through `blob:` URLs that its own script creates. Only same-origin script can create one, so this admits nothing from outside the page.
- `script-src` gained `'wasm-unsafe-eval'`: the viewer compiles a small WebAssembly decoder at start-up (used for meshopt-compressed models; this avatar is not one, but without the source the browser logs an error on every page load). The keyword permits WebAssembly compilation only; string `eval` stays forbidden, and only same-origin scripts can run at all.
- Still absent: `unsafe-inline`, `unsafe-eval`, `worker-src`, any `http:`/`https:` source. A test pins all of this.

To update either file: replace it, update the hash above, and re-run `tests/test_web.py` and `tests/test_ui_e2e.py`.

## The /arcade page (`cabinet.glb`, `three.min.js` and the loaders)

- `cabinet.glb`: "Game-ready arcade machine" by Snji (https://sketchfab.com/snji_), https://sketchfab.com/3d-models/game-ready-arcade-machine-25fb590707034cff8b555953d66a0f7b, CC BY 4.0 (the file's own metadata says so). The patch's notes say the original artwork was replaced with EventMate's own art; that is the patch author's change and was not independently checked beyond the file's licence metadata. Modified by EventMate: the 2048 px metallic-roughness texture was downscaled to 1024 px to halve the download (2.1 MB to 1.1 MB); nothing else was changed. SHA-256: `de45e67aa8889972fa17f415aeb7ac1b12f88a6550724e9cfc188714573b84c4`
- The attendant on that page is the same Pauly model as the chat avatar (`avatar.glb`, credited above); the patch shipped a byte-identical copy as `character.glb`, which was removed.
- `three.min.js` (three.js r128, MIT, licence in `three.min.LICENSE.txt`) with the standard EffectComposer, bloom and GLTFLoader add-ons. SHA-256 of `three.min.js`: `9274bbcec8d96168626c732b5d31c775aa8cfb7eaa0599bec0c175908a2c1ce2`
- Credit is shown at the bottom of the `/arcade` page and in `docs/ARCADE_UI.md`.
