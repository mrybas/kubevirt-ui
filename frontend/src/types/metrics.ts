export interface MetricsStatus {
  available: boolean;
  backend: string | null;
  // Node metrics cover the whole cluster: admins only.
  cluster?: boolean;
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
