/**
 * Global User Manual / Tour Initializer
 * Automatically activates on every page where topbar is present.
 */
import { initTourAutoLaunch } from './engine.js';

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    initTourAutoLaunch(1200);
  });
} else {
  initTourAutoLaunch(1200);
}
