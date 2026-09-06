/** Old receipts remain useful records, but never acquire authority over a new capture. */
export function receiptMatchesRapportArm(receiptWseq: number | undefined, armedWseq: number | null): boolean {
  return Number.isSafeInteger(receiptWseq) && Number(receiptWseq) > 0 && receiptWseq === armedWseq;
}
