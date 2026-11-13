const reportFilters = document.getElementById('report-filters');

for (const field of reportFilters.querySelectorAll('select')) {
  field.addEventListener('change', () => reportFilters.requestSubmit());
}
