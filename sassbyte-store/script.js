document.addEventListener('DOMContentLoaded', () => {
  const yearElement = document.getElementById('year');
  if (yearElement) yearElement.textContent = new Date().getFullYear();

  const contactForm = document.getElementById('contactForm');
  if (contactForm) {
    const submitButton = contactForm.querySelector('button[type="submit"]');

    contactForm.addEventListener('submit', async (event) => {
      event.preventDefault();

      if (!submitButton) return;

      const formData = new FormData(contactForm);
      const payload = {
        name: (formData.get('name') || '').toString().trim(),
        email: (formData.get('email') || '').toString().trim(),
        company: (formData.get('company') || '').toString().trim(),
        message: (formData.get('message') || '').toString().trim()
      };

      if (!payload.name || !payload.email || !payload.message) {
        alert('Please complete your name, email, and project details.');
        return;
      }

      submitButton.disabled = true;
      submitButton.textContent = 'Sending...';

      try {
        const response = await fetch('/api/contact', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        const result = await response.json().catch(() => ({ error: 'Request failed.' }));

        if (!response.ok) {
          throw new Error(result.error || 'Unable to send inquiry.');
        }

        alert('Your inquiry has been sent successfully.');
        contactForm.reset();
      } catch (error) {
        alert(error.message || 'Unable to send inquiry right now.');
      } finally {
        submitButton.disabled = false;
        submitButton.textContent = 'Send inquiry';
      }
    });
  }

  const menuToggle = document.querySelector('.menu-toggle');
  const nav = document.querySelector('.main-nav');

  if (menuToggle && nav) {
    menuToggle.addEventListener('click', () => {
      const isVisible = nav.style.display === 'flex';
      nav.style.display = isVisible ? 'none' : 'flex';
      nav.style.flexDirection = 'column';
      nav.style.position = 'absolute';
      nav.style.top = '78px';
      nav.style.left = '16px';
      nav.style.right = '16px';
      nav.style.padding = '18px';
      nav.style.border = '1px solid rgba(148, 163, 184, 0.2)';
      nav.style.borderRadius = '18px';
      nav.style.background = 'rgba(11, 18, 29, 0.94)';
    });
  }
});
