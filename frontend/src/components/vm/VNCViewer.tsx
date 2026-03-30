import { useRef, useState, useCallback } from 'react';
import { VncScreen, VncScreenHandle } from 'react-vnc';
import { Loader2, Monitor, AlertTriangle, Maximize2, Minimize2, Power } from 'lucide-react';

interface VNCViewerProps {
  namespace: string;
  vmName: string;
  isRunning: boolean;
}

export function VNCViewer({ namespace, vmName, isRunning }: VNCViewerProps) {
  const vncRef = useRef<VncScreenHandle>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<'connecting' | 'connected' | 'disconnected' | 'error'>('disconnected');
  const [errorMessage, setErrorMessage] = useState<string>('');
  const [isFullscreen, setIsFullscreen] = useState(false);

  // Build WebSocket URL to backend proxy
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  const wsUrl = `${protocol}//${host}/api/v1/namespaces/${namespace}/vms/${vmName}/console/vnc`;

  const handleConnect = useCallback(() => {
    console.log('VNC connected');
    setStatus('connected');
    setErrorMessage('');
  }, []);

  const handleDisconnect = useCallback(() => {
    console.log('VNC disconnected');
    setStatus('disconnected');
  }, []);

  const handleSecurityFailure = useCallback((e?: { detail: { status: number; reason: string } }) => {
    console.error('VNC security failure:', e?.detail);
    setStatus('error');
    setErrorMessage(e?.detail?.reason || 'Security failure');
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!containerRef.current) return;

    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen().then(() => {
        setIsFullscreen(true);
      }).catch(err => {
        console.error('Failed to enter fullscreen:', err);
      });
    } else {
      document.exitFullscreen().then(() => {
        setIsFullscreen(false);
      }).catch(err => {
        console.error('Failed to exit fullscreen:', err);
      });
    }
  }, []);

  const sendCtrlAltDel = useCallback(() => {
    vncRef.current?.sendCtrlAltDel();
  }, []);

  if (!isRunning) {
    return (
      <div className="flex flex-col items-center justify-center h-96 bg-surface-900 rounded-lg border border-surface-700">
        <Monitor className="h-16 w-16 text-surface-500 mb-4" />
        <p className="text-surface-400 text-lg">VM is not running</p>
        <p className="text-surface-500 text-sm mt-2">Start the VM to access the console</p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="relative bg-surface-900 rounded-lg overflow-hidden">
      {/* Toolbar */}
      <div className="absolute top-2 right-2 z-10 flex items-center gap-2">
        {status === 'connected' && (
          <>
            <button
              onClick={sendCtrlAltDel}
              className="p-2 bg-surface-800/80 hover:bg-surface-700 rounded-lg text-surface-300 hover:text-white transition-colors"
              title="Send Ctrl+Alt+Del"
            >
              <Power className="h-4 w-4" />
            </button>
            <button
              onClick={toggleFullscreen}
              className="p-2 bg-surface-800/80 hover:bg-surface-700 rounded-lg text-surface-300 hover:text-white transition-colors"
              title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            >
              {isFullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </button>
          </>
        )}
      </div>

      {/* Status indicator */}
      <div className="absolute top-2 left-2 z-10">
        <div className={`flex items-center gap-2 px-3 py-1.5 rounded-full text-sm ${
          status === 'connected' ? 'bg-green-500/20 text-green-400' :
          status === 'connecting' ? 'bg-yellow-500/20 text-yellow-400' :
          status === 'error' ? 'bg-red-500/20 text-red-400' :
          'bg-surface-500/20 text-surface-400'
        }`}>
          {status === 'connecting' && <Loader2 className="h-3 w-3 animate-spin" />}
          {status === 'connected' && <div className="h-2 w-2 bg-green-400 rounded-full" />}
          {status === 'error' && <AlertTriangle className="h-3 w-3" />}
          {status === 'disconnected' && <div className="h-2 w-2 bg-surface-400 rounded-full" />}
          <span className="capitalize">{status}</span>
        </div>
      </div>

      {/* Error message */}
      {status === 'error' && errorMessage && (
        <div className="absolute top-12 left-2 z-10 bg-red-500/20 text-red-400 px-3 py-2 rounded-lg text-sm max-w-md">
          {errorMessage}
        </div>
      )}

      {/* VNC Screen */}
      <VncScreen
        ref={vncRef}
        url={wsUrl}
        style={{
          width: '100%',
          height: '600px',
          background: '#1a1a2e',
        }}
        scaleViewport
        clipViewport={false}
        dragViewport={false}
        resizeSession
        showDotCursor
        autoConnect={true}
        retryDuration={5000}
        debug={false}
        rfbOptions={{
          shared: true,
        }}
        loadingUI={
          <div className="flex flex-col items-center justify-center h-full bg-surface-900">
            <Loader2 className="h-12 w-12 text-blue-500 animate-spin mb-4" />
            <p className="text-surface-400">Connecting to console...</p>
          </div>
        }
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
        onSecurityFailure={handleSecurityFailure}
      />
    </div>
  );
}
