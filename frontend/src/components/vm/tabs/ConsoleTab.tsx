import { useEffect } from 'react';
import { Monitor, Terminal } from 'lucide-react';
import { VNCViewer } from '@/components/vm/VNCViewer';
import { SerialConsole } from '@/components/vm/SerialConsole';

export function ConsoleTab({
  vm,
  consoleType,
  setConsoleType,
  isRunning,
}: {
  vm: any;
  consoleType: 'vnc' | 'serial';
  setConsoleType: (type: 'vnc' | 'serial') => void;
  isRunning: boolean;
}) {
  const vncEnabled = vm.console?.vnc_enabled ?? true;
  const serialEnabled = vm.console?.serial_console_enabled ?? false;
  const hasAnyConsole = vncEnabled || serialEnabled;
  
  // Auto-select available console if current is disabled
  useEffect(() => {
    if (consoleType === 'vnc' && !vncEnabled && serialEnabled) {
      setConsoleType('serial');
    } else if (consoleType === 'serial' && !serialEnabled && vncEnabled) {
      setConsoleType('vnc');
    }
  }, [consoleType, vncEnabled, serialEnabled]);
  
  if (!hasAnyConsole) {
    return (
      <div className="card">
        <div className="card-body text-center py-16">
          <Monitor className="h-12 w-12 mx-auto text-surface-600 mb-4" />
          <h3 className="text-lg font-medium text-surface-300 mb-2">No Console Available</h3>
          <p className="text-surface-500 max-w-md mx-auto">
            This VM has no console enabled. To enable VNC or Serial console, 
            edit the VM settings (requires VM restart).
          </p>
        </div>
      </div>
    );
  }
  
  return (
    <div className="space-y-4">
      {/* Console Type Selector - only show if both are available */}
      {vncEnabled && serialEnabled && (
        <div className="flex items-center justify-between">
          <div className="flex bg-surface-800 rounded-lg p-1">
            <button
              onClick={() => setConsoleType('vnc')}
              className={`flex items-center gap-2 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                consoleType === 'vnc'
                  ? 'bg-primary-500 text-white'
                  : 'text-surface-400 hover:text-surface-200'
              }`}
            >
              <Monitor className="h-4 w-4" />
              VNC Console
            </button>
            <button
              onClick={() => setConsoleType('serial')}
              className={`flex items-center gap-2 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                consoleType === 'serial'
                  ? 'bg-primary-500 text-white'
                  : 'text-surface-400 hover:text-surface-200'
              }`}
            >
              <Terminal className="h-4 w-4" />
              Serial Console
            </button>
          </div>
        </div>
      )}

      {/* Console Display */}
      <div className="card">
        <div className="card-body p-0">
          {consoleType === 'vnc' && vncEnabled ? (
            <VNCViewer
              namespace={vm.namespace}
              vmName={vm.name}
              isRunning={isRunning}
            />
          ) : serialEnabled ? (
            <SerialConsole
              namespace={vm.namespace}
              vmName={vm.name}
              isRunning={isRunning}
            />
          ) : null}
        </div>
      </div>

      {/* Console Tips */}
      <div className="bg-surface-800 border border-surface-700 rounded-lg p-4">
        <h4 className="font-medium text-surface-200 mb-2">Tips</h4>
        <p className="text-sm text-surface-400">
          {consoleType === 'vnc' && vncEnabled
            ? 'Click inside the console to capture keyboard input. Use Ctrl+Alt+Del from your OS menu if needed.'
            : 'Serial console provides a text-based interface. Useful for troubleshooting boot issues.'
          }
        </p>
        <div className="mt-3 flex gap-3">
          <code className="text-xs bg-surface-900 px-3 py-1.5 rounded text-primary-400">
            virtctl {consoleType === 'vnc' && vncEnabled ? 'vnc' : 'console'} {vm.name} -n {vm.namespace}
          </code>
        </div>
      </div>
    </div>
  );
}
