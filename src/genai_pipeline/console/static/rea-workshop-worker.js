import {describeCapabilities, matchAll, routeGoal} from '/engines/REACodeRefactoring/src/capabilities/registry.js';
function world(d){const cells=d.cells,top=m=>m===0?0:32-Math.clz32(m),id=c=>typeof c==='number'?c:c.id;
 const w={grid:{cells},cells,solid:Int32Array.from(d.solid),rock:Int32Array.from(d.rock),color:Int32Array.from(d.color),river:Uint8Array.from(d.river),version:0};
 w.top=c=>top(w.solid[id(c)]);w.ground=c=>top(w.rock[id(c)]);w.isRock=(c,h)=>!!(w.rock[id(c)]&(1<<(h-1)));w.isSolid=(c,h)=>!!(w.solid[id(c)]&(1<<(h-1)));return w;}
onmessage=({data})=>{try{let result;if(data.op==='catalog')result=describeCapabilities();else if(data.op==='scan'){const matches=matchAll(world(data.world),{}, {focusCells:data.focus||null,limit:8});result={matches,recommendations:routeGoal(data.goal,matches)}}else throw Error('未知操作');postMessage({id:data.id,result});}catch(e){postMessage({id:data.id,error:e.message});}};
