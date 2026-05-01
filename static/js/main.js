// Sidebar toggle for mobile
const toggleBtn = document.getElementById('sidebarToggle');
const sidebar = document.getElementById('sidebar');
if (toggleBtn && sidebar) {
  toggleBtn.addEventListener('click', () => sidebar.classList.toggle('open'));
  document.addEventListener('click', e => {
    if (!sidebar.contains(e.target) && e.target !== toggleBtn) {
      sidebar.classList.remove('open');
    }
  });
}

// Auto-dismiss alerts after 4s
document.querySelectorAll('.alert-dismissible').forEach(alert => {
  setTimeout(() => {
    const bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
    bsAlert.close();
  }, 4000);
});
