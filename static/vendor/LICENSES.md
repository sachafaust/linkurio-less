Vendored third-party files (unmodified):
- cytoscape.min.js 3.30.4 (MIT) https://github.com/cytoscape/cytoscape.js
  Local patch: four caches start as `{}` instead of `[]`: `propCache`, `styleCache`, `nodePathCache` and `arrowPathCache`.
  Each one is indexed by a 32-bit string hash, so an array gets a length near two billion.
  V8 then sometimes allocates a dense store of gigabytes, and Chrome kills the tab.
  3.35.0 has the same code. Apply the same four edits after an upgrade. tests/test_graphql.py checks them.
- layout-base.js 2.0.1, cose-base.js 2.2.0, cytoscape-fcose.js 2.2.0 (MIT) https://github.com/iVis-at-Bilkent
- Icon paths in ../icons.js come from @mdi/js 7.4.47 (Apache-2.0) https://pictogrammers.com
- fonts/Roboto (SIL OFL 1.1) https://fonts.google.com/specimen/Roboto
- graphology-fa2.js: an esbuild bundle (tools/fa2-entry.js) of graphology 0.26.0 and graphology-layout-forceatlas2 0.10.1 (both MIT, https://github.com/graphology/graphology), used for the ForceAtlas2 web-worker layout on large graphs. Rebuild: `npm i graphology graphology-layout-forceatlas2 esbuild && npx esbuild tools/fa2-entry.js --bundle --minify --format=iife --legal-comments=inline --outfile=static/vendor/graphology-fa2.js`
