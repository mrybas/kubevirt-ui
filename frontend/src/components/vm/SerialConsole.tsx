import { useEffect, useRef, useState, useCallback } from 'react';
import { Terminal } from 'xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import 'xterm/css/xterm.css';
import { Loader2, Terminal as TerminalIcon, AlertTriangle, Maximize2, Minimize2 } from 'lucide-react';

interface SerialConsoleProps {
  namespace: string;
  vmName: string;
  isRunning: boolean;
}

export function SerialConsole({ namespace, vmName, isRunning }: SerialConsoleProps) {
  const terminalRef = useRef<HTMLDivElement>(null);
  const terminalInstance = useRef<Terminal | null>(null);
  const fitAddon = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  
  const [status, setStatus] = useState<'connecting' | 'connected' | 'disconnected' | 'error'>('disconnected');
  const [errorMessage, setErrorMessage] = useState<string>('');
  const [isFullscreen, setIsFullscreen] = useState(false);

  const connect = useCallback(() => {
    if (!terminalRef.current || !isRunning) return;

    // Close existing connection
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    setStatus('connecting');
    setErrorMessage('');

    // Initialize terminal if not exists
    if (!terminalInstance.current) {
      const term = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        fontFamily: 'JetBrains Mono, Menlo, Monaco, Consolas, monospace',
        theme: {
          background: '#1a1a2e',
          foreground: '#e4e4e7',
          cursor: '#3b82f6',
          cursorAccent: '#1a1a2e',
          selectionBackground: '#3b82f680',
          black: '#27272a',
          red: '#ef4444',
          green: '#22c55e',
          yellow: '#eab308',
          blue: '#3b82f6',
          magenta: '#a855f7',
          cyan: '#06b6d4',
          white: '#e4e4e7',
          brightBlack: '#52525b',
          brightRed: '#f87171',
          brightGreen: '#4ade80',
          brightYellow: '#facc15',
          brightBlue: '#60a5fa',
          brightMagenta: '#c084fc',
          brightCyan: '#22d3ee',
          brightWhite: '#fafafa',
        },
      });

      fitAddon.current = new FitAddon();
      term.loadAddon(fitAddon.current);
      term.loadAddon(new WebLinksAddon());

      term.open(terminalRef.current);
      fitAddon.current.fit();

      terminalInstance.current = term;
    } else {
      terminalInstance.current.clear();
    }

    // Build WebSocket URL
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const wsUrl = `${protocol}//${host}/api/v1/namespaces/${namespace}/vms/${vmName}/console/serial`;

    // Connect WebSocket
    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer'; // Use arraybuffer for better UTF-8 handling
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus('connected');
      terminalInstance.current?.writeln('\x1b[32m● Connected to serial console\x1b[0m');
      terminalInstance.current?.writeln('\x1b[90mPress Enter to activate the console...\x1b[0m');
      terminalInstance.current?.writeln('');
    };

    ws.onmessage = (event) => {
      if (terminalInstance.current) {
        if (event.data instanceof Blob) {
          // Read blob as UTF-8 text
          const reader = new FileReader();
          reader.onload = () => {
            if (typeof reader.result === 'string') {
              terminalInstance.current?.write(reader.result);
            }
          };
          reader.readAsText(event.data, 'UTF-8');
        } else if (event.data instanceof ArrayBuffer) {
          // Decode ArrayBuffer as UTF-8
          const decoder = new TextDecoder('utf-8');
          terminalInstance.current.write(decoder.decode(event.data));
        } else {
          terminalInstance.current.write(event.data);
        }
      }
    };

    ws.onclose = (event) => {
      setStatus('disconnected');
      if (event.code !== 1000) {
        terminalInstance.current?.writeln('\x1b[31m● Disconnected from serial console\x1b[0m');
      }
    };

    ws.onerror = () => {
      setStatus('error');
      setErrorMessage('Connection failed');
      terminalInstance.current?.writeln('\x1b[31m● Connection error\x1b[0m');
    };

    // Handle terminal input
    terminalInstance.current.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(data);
      }
    });
  }, [namespace, vmName, isRunning]);

  const disconnect = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setStatus('disconnected');
  }, []);

  // Connect when VM is running
  useEffect(() => {
    if (isRunning) {
      connect();
    } else {
      disconnect();
    }

    return () => {
      disconnect();
      if (terminalInstance.current) {
        terminalInstance.current.dispose();
        terminalInstance.current = null;
      }
    };
  }, [isRunning, connect, disconnect]);

  // Handle resize
  useEffect(() => {
    const handleResize = () => {
      if (fitAddon.current) {
        fitAddon.current.fit();
      }
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Handle fullscreen
  useEffect(() => {
    const handleFullscreenChange = () => {
      setIsFullscreen(!!document.fullscreenElement);
      // Refit terminal after fullscreen change
      setTimeout(() => {
        if (fitAddon.current) {
          fitAddon.current.fit();
        }
      }, 100);
    };

    document.addEventListener('fullscreenchange', handleFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!containerRef.current) return;

    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen();
    } else {
      document.exitFullscreen();
    }
  }, []);

  if (!isRunning) {
    return (
      <div className="flex flex-col items-center justify-center h-96 bg-surface-900 rounded-lg border border-surface-700">
        <TerminalIcon className="h-16 w-16 text-surface-500 mb-4" />
        <p className="text-surface-400 text-lg">VM is not running</p>
        <p className="text-surface-500 text-sm mt-2">Start the VM to access the serial console</p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="relative bg-surface-900 rounded-lg overflow-hidden">
      {/* Toolbar */}
      <div className="absolute top-2 right-2 z-10 flex items-center gap-2">
        {status === 'disconnected' && (
          <button
            onClick={connect}
            className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 rounded-lg text-white text-sm transition-colors"
          >
            Reconnect
          </button>
        )}
        <button
          onClick={toggleFullscreen}
          className="p-2 bg-surface-800/80 hover:bg-surface-700 rounded-lg text-surface-300 hover:text-white transition-colors"
          title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
        >
          {isFullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
        </button>
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

      {/* Terminal */}
      <div 
        ref={terminalRef} 
        className="p-4 pt-12"
        style={{ height: '500px' }}
      />
    </div>
  );
}
