import { pathToFileURL } from 'node:url';
export async function resolve(specifier, context, next) {
  if (specifier === 'three') return {url: pathToFileURL(process.env.GENAI_REA_THREE).href, shortCircuit: true};
  return next(specifier, context);
}
