export interface AsyncOperationFence {
  begin: () => number;
  invalidate: () => void;
  isCurrent: (generation: number) => boolean;
}

export function createAsyncOperationFence(): AsyncOperationFence {
  let generation = 0;
  return {
    begin: () => {
      generation += 1;
      return generation;
    },
    invalidate: () => { generation += 1; },
    isCurrent: (candidate) => candidate === generation,
  };
}

export async function deliverIfCurrent<T>(
  fence: AsyncOperationFence,
  generation: number,
  load: () => Promise<T>,
  deliver: (value: T) => void,
): Promise<boolean> {
  const value = await load();
  if (!fence.isCurrent(generation)) return false;
  deliver(value);
  return true;
}
