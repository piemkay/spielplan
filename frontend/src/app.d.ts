declare global {
  namespace App {
    interface PageState {
      /** Open sheets, innermost last: each is a history entry that Back closes (decision 527). */
      sheets?: string[];
      /** The card a jump to Home left from, reopened on Back (decision 557 item 6). */
      returnCard?: { titleId: number; from: string };
    }
  }
}

export {};
