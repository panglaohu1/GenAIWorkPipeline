/** Bridge the learned planet capability contracts to the local sphere pipeline. */
import { PLANET_CAPABILITIES } from '../../../engines/system-workshop/planet/capabilities.js';

import {TERRAIN_EDIT_CAPABILITIES} from '../../../engines/system-workshop/planet/terrain-edit.js';
const source = 'engines/system-workshop/planet/capabilities.js';
export function catalog() {
  return PLANET_CAPABILITIES.map(c => ({...c.meta, source,
    executable: typeof c.run === 'function',
    integration: typeof c.run === 'function' ? 'recipe' : 'runtime-adapter-required'})).concat(TERRAIN_EDIT_CAPABILITIES.map(c=>({...c,executable:false,editorExecutable:true,integration:'sphere-snapshot-editor'}))).concat([{id:'planet.lake-town',name:'湖面水上建筑',source:'engines/REACodeRefactoring/src/sphere/planet-town.js#storeyHouseModule',adapter:'engines/system-workshop/planet/lake-town.js',executable:false,editorExecutable:true,integration:'version-bound-lake-scene'}]);
}
export function execute(recipe, steps = []) {
  if (!Array.isArray(steps) || steps.length > 16) throw Error('球系统能力步骤应为最多16项的数组');
  let current = {...recipe};
  const trace = [];
  for (const step of steps) {
    const c = PLANET_CAPABILITIES.find(c => c.meta.id === step.id);
    if (!c || typeof c.run !== 'function') throw Error('球系统尚未接入此能力执行器：' + step.id);
    const params = {}, supplied = step.params || {};
    for (const key of Object.keys(supplied)) if (!(key in c.meta.params)) throw Error('未知能力参数：' + key);
    for (const [key, spec] of Object.entries(c.meta.params)) {
      const v = supplied[key] ?? spec.default;
      if (typeof v !== 'number' || !Number.isFinite(v) || v < spec.min || v > spec.max) throw Error('能力参数越界：' + key);
      params[key] = v;
    }
    current = c.run(current, params);
    trace.push({id: c.meta.id, name: c.meta.name, source, params,
      match: {status: 'not_scanned', reason: '首次构建没有前置球面快照；由编译器检查配方与地形约束'},
      before: trace.length ? trace.at(-1).after : recipe, after: {...current}});
  }
  return {recipe: current, trace};
}
if (process.argv.includes('--catalog')) console.log(JSON.stringify(catalog()));
