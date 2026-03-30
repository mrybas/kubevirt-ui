import { Check } from 'lucide-react';
import { useMediaQuery } from '@/hooks/useMediaQuery';

interface Props {
  steps: string[];
  currentStep: number;
  onStepClick?: (step: number) => void;
}

export function WizardStepIndicator({ steps, currentStep, onStepClick }: Props) {
  const isMobile = useMediaQuery('(max-width: 768px)');

  return (
    <div className="flex items-center justify-center gap-0">
      {steps.map((label, idx) => {
        const isCompleted = idx < currentStep;
        const isCurrent = idx === currentStep;

        return (
          <div key={label} className="flex items-center">
            <button
              type="button"
              onClick={() => onStepClick?.(idx)}
              disabled={!onStepClick}
              className="flex flex-col items-center gap-1.5 group"
            >
              <div
                className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium transition-colors ${
                  isCompleted
                    ? 'bg-emerald-500 text-white'
                    : isCurrent
                    ? 'bg-primary-500 text-white ring-2 ring-primary-500/30'
                    : 'bg-surface-700 text-surface-400'
                }`}
              >
                {isCompleted ? <Check className="w-4 h-4" /> : idx + 1}
              </div>
              {!isMobile && (
                <span
                  className={`text-xs whitespace-nowrap transition-colors ${
                    isCurrent
                      ? 'text-primary-300 font-medium'
                      : isCompleted
                      ? 'text-emerald-400'
                      : 'text-surface-500'
                  }`}
                >
                  {label}
                </span>
              )}
            </button>

            {idx < steps.length - 1 && (
              <div
                className={`${isMobile ? 'w-4' : 'w-8'} h-0.5 mx-1 ${isMobile ? 'mb-0' : 'mb-4'} transition-colors ${
                  idx < currentStep ? 'bg-emerald-500' : 'bg-surface-700'
                }`}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
