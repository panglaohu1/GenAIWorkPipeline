import fs from 'node:fs';
import {editTerrain} from '../../../engines/system-workshop/planet/terrain-edit.js';
const {input,out,operation}=JSON.parse(fs.readFileSync(0,'utf8'));
const data=editTerrain(JSON.parse(fs.readFileSync(input,'utf8')),operation);
if(!data.checks.finite||!data.checks.closed||data.checks.euler!==2)throw Error('地形检查未通过');
fs.writeFileSync(out,JSON.stringify(data));console.log(JSON.stringify({revision:data.editRevision,trace:data.capabilities.at(-1)}));
