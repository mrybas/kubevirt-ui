import { Check, Copy, FileCode } from 'lucide-react';

export function YamlTab({
  vmYaml,
  onCopy,
  copied,
}: {
  vmYaml: any;
  onCopy: () => void;
  copied: boolean;
}) {
  return (
    <div className="card">
      <div className="card-header flex items-center justify-between">
        <h3 className="font-display text-lg font-semibold">VM Manifest</h3>
        <div className="flex gap-2">
          <button onClick={onCopy} className="btn-ghost text-sm">
            {copied ? <Check className="h-4 w-4 text-emerald-400" /> : <Copy className="h-4 w-4" />}
            {copied ? 'Copied!' : 'Copy'}
          </button>
          <button className="btn-secondary text-sm">
            <FileCode className="h-4 w-4" />
            Edit YAML
          </button>
        </div>
      </div>
      <div className="card-body p-0">
        <pre className="bg-surface-950 rounded-lg p-4 overflow-auto max-h-[600px] text-sm font-mono text-surface-300 m-4">
          {vmYaml ? JSON.stringify(vmYaml, null, 2) : 'Loading...'}
        </pre>
      </div>
    </div>
  );
}
