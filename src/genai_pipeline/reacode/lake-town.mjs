import fs from 'node:fs';
import {buildLakeTown} from '../../../engines/system-workshop/planet/lake-town.js';
const {input,out,options}=JSON.parse(fs.readFileSync(0,'utf8'));
const result=buildLakeTown(JSON.parse(fs.readFileSync(input,'utf8')),options);
if(!result.checks.finite||!result.checks.allDecksAboveWater||!result.checks.allFoundationsSupported)throw Error('湖区建筑检查失败');
fs.writeFileSync(out,JSON.stringify(result));console.log(JSON.stringify({buildings:result.buildings.length,checks:result.checks}));
