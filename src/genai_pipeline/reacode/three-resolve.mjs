/**
 * three-resolve.mjs — Node 侧把 `three` 解析到仓库自带的 vendor 模块。
 *
 * WorldMesh / MeshWriter 是浏览器模块，只用到 THREE.Color 和 BufferGeometry 这类纯数据部分；
 * 服务端烘球面网格时通过 register() 挂上这个 resolver，就能复用同一份源码，
 * 不必为 node 再抄一份网格生成。
 */
export async function resolve(specifier, context, nextResolve) {
  if (specifier === 'three') {
    return { url: new URL('../../../engines/REACodeRefactoring/vendor/three.module.js', import.meta.url).href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
