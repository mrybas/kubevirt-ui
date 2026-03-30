import { useState } from 'react';
import { Copy, Check } from 'lucide-react';

interface CopyableValueProps {
  value: string | undefined;
  fallback?: string;
  className?: string;
}

export function CopyableValue({ value, fallback = '-', className }: CopyableValueProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const el = document.createElement('textarea');
      el.value = value;
      el.style.position = 'fixed';
      el.style.opacity = '0';
      document.body.appendChild(el);
      el.select();
      document.execCommand('copy');
      document.body.removeChild(el);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (!value) {
    return <span className={className}>{fallback}</span>;
  }

  return (
    <button
      onClick={handleCopy}
      className={`font-mono hover:text-primary-400 transition-colors flex items-center gap-1.5 group ${className ?? ''}`}
      title="Click to copy"
    >
      {value}
      {copied ? (
        <Check className="h-3.5 w-3.5 text-emerald-400" />
      ) : (
        <Copy className="h-3.5 w-3.5 opacity-0 group-hover:opacity-100 transition-opacity" />
      )}
    </button>
  );
}
