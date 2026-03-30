import { useState } from 'react';

export function usePagination(initialPerPage = 50) {
  const [page, setPageState] = useState(1);
  const [perPage, setPerPageState] = useState(initialPerPage);

  const setPage = (p: number) => setPageState(Math.max(1, p));

  const setPerPage = (pp: number) => {
    setPerPageState(pp);
    setPageState(1);
  };

  return { page, perPage, setPage, setPerPage };
}
