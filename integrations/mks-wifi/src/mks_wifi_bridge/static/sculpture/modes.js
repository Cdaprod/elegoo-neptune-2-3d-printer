export const MODE_REGISTRY=Object.freeze({
 ambient:{title:'Industrial nocturne',subtitle:'Form, material and quiet structure',color:0x96d7ed},
 architecture:{title:'Architecture of motion',subtitle:'Nominal envelope · source-qualified kinematics',color:0x65dcf5},
 thermodynamics:{title:'Thermodynamics',subtitle:'Procedural thermal approximation · not a measured spatial field',color:0xffae65},
 fabrication:{title:'Matter, layer by layer',subtitle:'G-code reconstruction · file-progress estimates are inferred',color:0xffd39a},
 intelligence:{title:'The shape of a signal',subtitle:'Temperature histories, regulation error and observed communication',color:0x7ddfd7},
});
export function createModeWeights(){return Object.fromEntries(Object.keys(MODE_REGISTRY).map(k=>[k,k==='ambient'?1:0]));}
export function transitionModes(weights,target,delta,reduced=false){for(const key in weights)weights[key]+=(Number(key===target)-weights[key])*(reduced?1:1-Math.exp(-delta*1.8));return weights;}
