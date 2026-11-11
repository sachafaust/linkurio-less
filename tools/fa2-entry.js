import Graph from 'graphology';
import FA2Layout from 'graphology-layout-forceatlas2/worker';
import forceAtlas2 from 'graphology-layout-forceatlas2';
window.GXFA2 = { Graph, FA2Layout, inferSettings: forceAtlas2.inferSettings };
