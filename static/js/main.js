// Off-canvas sidebar for mobile (drawer + backdrop + scroll lock)
const toggleBtn = document.getElementById('sidebarToggle');
const sidebar = document.getElementById('sidebar');
if (toggleBtn && sidebar) {
  // Backdrop behind the drawer (created once)
  let backdrop = document.querySelector('.sidebar-backdrop');
  if (!backdrop) {
    backdrop = document.createElement('div');
    backdrop.className = 'sidebar-backdrop';
    document.body.appendChild(backdrop);
  }

  const openSidebar = () => {
    sidebar.classList.add('open');
    backdrop.classList.add('show');
    document.body.classList.add('sidebar-open');
    toggleBtn.setAttribute('aria-expanded', 'true');
  };
  const closeSidebar = () => {
    sidebar.classList.remove('open');
    backdrop.classList.remove('show');
    document.body.classList.remove('sidebar-open');
    toggleBtn.setAttribute('aria-expanded', 'false');
  };

  toggleBtn.setAttribute('aria-controls', 'sidebar');
  toggleBtn.setAttribute('aria-expanded', 'false');
  toggleBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    sidebar.classList.contains('open') ? closeSidebar() : openSidebar();
  });

  backdrop.addEventListener('click', closeSidebar);
  // Close when a nav link is tapped (navigating away on mobile)
  sidebar.querySelectorAll('a').forEach(a => a.addEventListener('click', closeSidebar));
  // Escape closes the drawer
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSidebar(); });
  // If resized up to desktop, make sure the drawer state is reset
  window.addEventListener('resize', () => { if (window.innerWidth > 991.98) closeSidebar(); });
}

// Auto-dismiss alerts after 4s
document.querySelectorAll('.alert-dismissible').forEach(alert => {
  setTimeout(() => {
    const bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
    bsAlert.close();
  }, 4000);
});
