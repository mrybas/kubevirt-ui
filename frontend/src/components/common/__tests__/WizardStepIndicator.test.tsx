import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WizardStepIndicator } from '../WizardStepIndicator';

const steps = ['General', 'Resources', 'Network', 'Review'];

describe('WizardStepIndicator', () => {
  it('renders all step labels', () => {
    render(<WizardStepIndicator steps={steps} currentStep={0} />);

    for (const step of steps) {
      expect(screen.getByText(step)).toBeInTheDocument();
    }
  });

  it('shows step numbers for non-completed steps', () => {
    render(<WizardStepIndicator steps={steps} currentStep={1} />);

    // Step 2 is current (idx 1), steps 3 and 4 are future — they show numbers
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();
  });

  it('highlights current step with primary styling', () => {
    render(<WizardStepIndicator steps={steps} currentStep={2} />);

    const networkLabel = screen.getByText('Network');
    expect(networkLabel).toHaveClass('text-primary-300');
  });

  it('shows completed steps with emerald styling', () => {
    render(<WizardStepIndicator steps={steps} currentStep={2} />);

    const generalLabel = screen.getByText('General');
    expect(generalLabel).toHaveClass('text-emerald-400');
  });

  it('shows future steps with muted styling', () => {
    render(<WizardStepIndicator steps={steps} currentStep={0} />);

    const reviewLabel = screen.getByText('Review');
    expect(reviewLabel).toHaveClass('text-surface-500');
  });

  it('calls onStepClick when a step is clicked', async () => {
    const onClick = vi.fn();
    render(
      <WizardStepIndicator steps={steps} currentStep={0} onStepClick={onClick} />,
    );

    await userEvent.click(screen.getByText('Resources'));
    expect(onClick).toHaveBeenCalledWith(1);
  });

  it('disables step buttons when no onStepClick provided', () => {
    render(<WizardStepIndicator steps={steps} currentStep={0} />);

    const buttons = screen.getAllByRole('button');
    buttons.forEach((btn) => expect(btn).toBeDisabled());
  });
});
