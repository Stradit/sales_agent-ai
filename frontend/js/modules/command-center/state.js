export const ccState = {
  activeRole: 'ae', // 'ae' | 'manager' | 'exec'
  activeDomainFilters: new Set(), // empty = all domains
  activeAccountId: null, // single / primary active account ID (backwards compatibility)
  activeAccountIds: new Set(), // Set of active account IDs for multi-selection
  selectedAccountName: null, // primary active account name
  selectedAccountNames: [], // Array of active account names
  selectedAccountObj: null, // primary active account object
  selectedAccountObjs: [], // Array of active account objects
  drawerAccountId: null,
  timelineFilter: 'all', // 'all' | 'joined' | 'promoted' | 'aging' | 'other'
  matrixChart: null,
};

/**
 * Checks if a given account ID or account Name matches the current active filter.
 * Returns true if no filter is active, or if the account matches any of the selected accounts.
 */
export function matchesCurrentAccount(accountId, accountName) {
  const hasMulti = ccState.activeAccountIds && ccState.activeAccountIds.size > 0;
  const hasSingle = Boolean(ccState.activeAccountId);

  if (!hasMulti && !hasSingle) return true;

  if (accountId !== undefined && accountId !== null) {
    const idStr = String(accountId);
    if (hasMulti) {
      for (const id of ccState.activeAccountIds) {
        if (String(id) === idStr) return true;
      }
    }
    if (hasSingle && String(ccState.activeAccountId) === idStr) {
      return true;
    }
  }

  if (accountName) {
    const lowerName = String(accountName).toLowerCase().trim();

    if (ccState.selectedAccountObjs && ccState.selectedAccountObjs.length > 0) {
      for (const obj of ccState.selectedAccountObjs) {
        const targetName = (obj.name || obj.display_name || '').toLowerCase().trim();
        if (targetName && (lowerName.includes(targetName) || targetName.includes(lowerName))) {
          return true;
        }
      }
    }

    if (ccState.selectedAccountNames && ccState.selectedAccountNames.length > 0) {
      for (const name of ccState.selectedAccountNames) {
        const targetName = String(name).toLowerCase().trim();
        if (targetName && (lowerName.includes(targetName) || targetName.includes(lowerName))) {
          return true;
        }
      }
    }

    if (ccState.selectedAccountName) {
      const targetName = String(ccState.selectedAccountName).toLowerCase().trim();
      if (targetName && (lowerName.includes(targetName) || targetName.includes(lowerName))) {
        return true;
      }
    }
  }

  return false;
}

