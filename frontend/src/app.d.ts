declare global {
  namespace App {
    interface PageState {
      /** Open sheets, innermost last: each is a history entry that Back closes (decision 527). */
      sheets?: string[];
    }
  }
}

export {};
