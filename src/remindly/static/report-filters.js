const reportFilters = document.getElementById('report-filters');

for (const field of reportFilters.querySelectorAll('select')) {
  field.addEventListener('change', () => reportFilters.requestSubmit());
}

reportFilters.querySelector('button[type="submit"]').hidden = true;
