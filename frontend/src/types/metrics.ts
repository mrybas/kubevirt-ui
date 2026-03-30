export interface MetricsStatus {
  available: boolean;
  backend: string | null;
}

export interface PromQLSeries {
  metric: Record<string, string>;
  value?: [number, string];    // instant query
  values?: [number, string][]; // range query
}

export interface PromQLResult {
  status: string;
  data: {
    resultType: 'vector' | 'matrix';
    result: PromQLSeries[];
  };
}
