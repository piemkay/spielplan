declare global {
  namespace App {
    interface PageState {
      /** Open sheets, innermost last: each is a history entry that Back closes (decision 527). */
      sheets?: string[];
      /** The card a jump to Home left from, reopened on Back (decision 557 item 6). */
      returnCard?: { titleId: number; from: string };
      /** On Home's entry after such a jump: the page it left, for Home's own way back. */
      jumpedFrom?: string;
    }
  }
}

export {};
