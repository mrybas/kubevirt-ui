import { useState, useEffect } from 'react';
import {
  Download,
  Copy,
  Check,
  Terminal,
  Loader2,
  Server,
  User,
  Shield,
  AlertTriangle,
  FileCode,
  Zap,
  Lock,
} from 'lucide-react';
import { getKubeconfig, type KubeconfigResponse, type KubeconfigVariant } from '@/api/auth';
import { useAuthStore } from '@/store/auth';
import { PageTitle } from '@/components/common/PageTitle';

export default function CLIAccess() {
  const [data, setData] = useState<KubeconfigResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedVariant, setSelectedVariant] = useState(0);
  const [showInstructions, setShowInstructions] = useState(false);
  const { config, idToken, refreshToken } = useAuthStore();

  useEffect(() => {
    async function fetchKubeconfig() {
      try {
        setLoading(true);
        setError('');
        const result = await getKubeconfig({
          id_token: idToken || undefined,
          refresh_token: refreshToken || undefined,
        });
        setData(result);
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : 'Failed to generate kubeconfig');
      } finally {
        setLoading(false);
      }
    }
    fetchKubeconfig();
  }, []);

  if (loading) {
    return (
      <div className="p-6 flex items-center justify-center min-h-[60vh]">
        <div className="text-center">
          <Loader2 className="h-8 w-8 text-primary-400 animate-spin mx-auto mb-3" />
          <p className="text-surface-400">Generating kubeconfig...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <div className="max-w-3xl mx-auto">
          <div className="card">
            <div className="card-body text-center py-16">
              <AlertTriangle className="h-12 w-12 text-amber-400 mx-auto mb-4" />
              <h2 className="text-xl font-semibold text-surface-100 mb-2">Cannot Generate Kubeconfig</h2>
              <p className="text-surface-400 mb-4">{error}</p>
              {config?.type === 'none' && (
                <p className="text-surface-500 text-sm">
                  Kubeconfig generation requires OIDC or token-based authentication.
                  Currently running in no-auth mode.
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!data || data.variants.length === 0) return null;

  const variant = data.variants[selectedVariant]!;

  const handleCopyTop = async () => {
    try {
      await navigator.clipboard.writeText(variant.kubeconfig);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = variant.kubeconfig;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
    }
  };

  const handleDownloadTop = () => {
    const blob = new Blob([variant.kubeconfig], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `kubeconfig-kubevirt-${variant.id}.yaml`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="p-6">
      <div className="max-w-4xl mx-auto space-y-6">
        <PageTitle title="CLI Access" subtitle="Download your kubeconfig to access the cluster via kubectl and Terraform">
          <button onClick={handleCopyTop} className="btn-secondary text-sm flex items-center gap-2">
            <Copy className="h-4 w-4" />
            Copy
          </button>
          <button onClick={handleDownloadTop} className="btn-primary text-sm flex items-center gap-2">
            <Download className="h-4 w-4" />
            Download
          </button>
        </PageTitle>

        {/* Info cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="card card-body flex items-start gap-3">
            <div className="p-2 rounded-lg bg-primary-500/10">
              <Server className="h-5 w-5 text-primary-400" />
            </div>
            <div>
              <p className="text-xs text-surface-500">Cluster</p>
              <p className="text-sm font-medium text-surface-200 font-mono break-all">{data.server}</p>
            </div>
          </div>
          <div className="card card-body flex items-start gap-3">
            <div className="p-2 rounded-lg bg-emerald-500/10">
              <User className="h-5 w-5 text-emerald-400" />
            </div>
            <div>
              <p className="text-xs text-surface-500">User</p>
              <p className="text-sm font-medium text-surface-200">{data.username}</p>
            </div>
          </div>
          <div className="card card-body flex items-start gap-3">
            <div className="p-2 rounded-lg bg-amber-500/10">
              <Shield className="h-5 w-5 text-amber-400" />
            </div>
            <div>
              <p className="text-xs text-surface-500">Auth</p>
              <p className="text-sm font-medium text-surface-200 uppercase">{data.auth_type}</p>
            </div>
          </div>
        </div>

        {/* Variant selector (only if multiple variants) */}
        {data.variants.length > 1 && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {data.variants.map((v, i) => (
              <button
                key={v.id}
                onClick={() => { setSelectedVariant(i); setShowInstructions(false); }}
                className={`text-left p-4 rounded-xl border-2 transition-all ${
                  selectedVariant === i
                    ? 'border-primary-500 bg-primary-500/5'
                    : 'border-surface-700 bg-surface-800 hover:border-surface-600'
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  {v.id === 'sa-token' ? (
                    <Zap className={`h-4 w-4 ${selectedVariant === i ? 'text-primary-400' : 'text-surface-400'}`} />
                  ) : (
                    <Lock className={`h-4 w-4 ${selectedVariant === i ? 'text-primary-400' : 'text-surface-400'}`} />
                  )}
                  <span className={`font-medium text-sm ${selectedVariant === i ? 'text-primary-400' : 'text-surface-200'}`}>
                    {v.label}
                  </span>
                  {v.id === 'sa-token' && (
                    <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-medium">
                      RECOMMENDED
                    </span>
                  )}
                </div>
                <p className="text-xs text-surface-400 leading-relaxed">{v.description}</p>
              </button>
            ))}
          </div>
        )}

        {/* Kubeconfig card */}
        <VariantCard
          variant={variant}
          showInstructions={showInstructions}
          onToggleInstructions={() => setShowInstructions(!showInstructions)}
        />
      </div>
    </div>
  );
}

function VariantCard({
  variant,
  showInstructions,
  onToggleInstructions,
}: {
  variant: KubeconfigVariant;
  showInstructions: boolean;
  onToggleInstructions: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(variant.kubeconfig);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = variant.kubeconfig;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const handleDownload = () => {
    const blob = new Blob([variant.kubeconfig], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `kubeconfig-kubevirt-${variant.id}.yaml`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="card">
      {/* Tab bar */}
      <div className="border-b border-surface-700">
        <div className="flex">
          <button
            onClick={() => { if (showInstructions) onToggleInstructions(); }}
            className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors ${
              !showInstructions
                ? 'border-primary-500 text-primary-400'
                : 'border-transparent text-surface-400 hover:text-surface-200'
            }`}
          >
            <FileCode className="h-4 w-4 inline-block mr-1.5 -mt-0.5" />
            Kubeconfig
          </button>
          <button
            onClick={() => { if (!showInstructions) onToggleInstructions(); }}
            className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors ${
              showInstructions
                ? 'border-primary-500 text-primary-400'
                : 'border-transparent text-surface-400 hover:text-surface-200'
            }`}
          >
            <Terminal className="h-4 w-4 inline-block mr-1.5 -mt-0.5" />
            Instructions
          </button>
        </div>
      </div>

      {!showInstructions ? (
        <div>
          {/* Action buttons */}
          <div className="flex items-center gap-2 p-4 border-b border-surface-700">
            <button onClick={handleCopy} className="btn-secondary text-sm">
              {copied ? (
                <>
                  <Check className="h-4 w-4 text-emerald-400" />
                  Copied!
                </>
              ) : (
                <>
                  <Copy className="h-4 w-4" />
                  Copy
                </>
              )}
            </button>
            <button onClick={handleDownload} className="btn-primary text-sm">
              <Download className="h-4 w-4" />
              Download
            </button>
          </div>

          {/* Kubeconfig content */}
          <div className="overflow-auto max-h-[500px]">
            <pre className="p-4 text-sm font-mono text-surface-300 leading-relaxed whitespace-pre">
              {variant.kubeconfig}
            </pre>
          </div>
        </div>
      ) : (
        <div className="p-6 prose prose-invert prose-sm max-w-none">
          <InstructionsRenderer markdown={variant.instructions} />
        </div>
      )}
    </div>
  );
}

function InstructionsRenderer({ markdown }: { markdown: string }) {
  // Simple markdown-to-JSX renderer for the instructions
  const lines = markdown.split('\n');
  const elements: React.ReactNode[] = [];
  let inCodeBlock = false;
  let codeLines: string[] = [];
  let codeLang = '';

  const flushCode = () => {
    if (codeLines.length > 0) {
      const code = codeLines.join('\n');
      elements.push(
        <div key={elements.length} className="my-3">
          <div className="bg-surface-900/70 rounded-lg overflow-hidden">
            {codeLang && (
              <div className="px-3 py-1 text-[10px] text-surface-500 uppercase tracking-wider border-b border-surface-700">
                {codeLang}
              </div>
            )}
            <pre className="p-3 text-sm font-mono text-surface-300 overflow-x-auto whitespace-pre">
              {code}
            </pre>
          </div>
        </div>
      );
      codeLines = [];
      codeLang = '';
    }
  };

  for (const line of lines) {
    if (line.startsWith('```')) {
      if (inCodeBlock) {
        flushCode();
        inCodeBlock = false;
      } else {
        inCodeBlock = true;
        codeLang = line.slice(3).trim();
      }
      continue;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      continue;
    }

    if (line.startsWith('## ')) {
      elements.push(
        <h2 key={elements.length} className="text-xl font-bold text-surface-100 mt-6 mb-3">
          {line.slice(3)}
        </h2>
      );
    } else if (line.startsWith('### ')) {
      elements.push(
        <h3 key={elements.length} className="text-base font-semibold text-surface-200 mt-4 mb-2">
          {line.slice(4)}
        </h3>
      );
    } else if (line.startsWith('**') && line.endsWith('**')) {
      elements.push(
        <p key={elements.length} className="text-surface-200 font-medium mt-2">
          {line.slice(2, -2)}
        </p>
      );
    } else if (line.trim()) {
      // Handle inline code
      const parts = line.split(/`([^`]+)`/);
      elements.push(
        <p key={elements.length} className="text-surface-400 leading-relaxed">
          {parts.map((part, i) =>
            i % 2 === 1 ? (
              <code key={i} className="px-1.5 py-0.5 bg-surface-700 rounded text-surface-200 text-xs font-mono">
                {part}
              </code>
            ) : (
              part
            )
          )}
        </p>
      );
    }
  }

  flushCode();
  return <>{elements}</>;
}
