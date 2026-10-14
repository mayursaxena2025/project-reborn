require('dotenv').config();

const express = require('express');
const nodemailer = require('nodemailer');

const app = express();
const port = Number(process.env.PORT || 3000);
const recipient = process.env.MAIL_TO || 'sassbyte.store@gmail.com';

app.use(express.json({ limit: '1mb' }));
app.use(express.urlencoded({ extended: true }));

function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value || '');
}

function buildEmailText({ name, email, company, message }) {
  const lines = [
    `Name: ${name || 'Not provided'}`,
    `Email: ${email || 'Not provided'}`,
    company ? `Company: ${company}` : 'Company: Not provided',
    '',
    'Project details:',
    message || 'No message provided.'
  ];

  return lines.join('\n');
}

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', service: 'sassbyte-contact-api' });
});

app.post('/api/contact', async (req, res) => {
  const { name, email, company, message } = req.body || {};

  if (!name || !email || !message) {
    return res.status(400).json({ error: 'Name, email, and message are required.' });
  }

  if (!isValidEmail(email)) {
    return res.status(400).json({ error: 'Please provide a valid email address.' });
  }

  const smtpUser = process.env.SMTP_USER;
  const smtpPass = process.env.SMTP_PASS;

  if (!smtpUser || !smtpPass) {
    return res.status(503).json({
      error: 'Email delivery is not configured. Add SMTP_USER and SMTP_PASS in the backend environment.'
    });
  }

  try {
    const transporter = nodemailer.createTransport({
      host: process.env.SMTP_HOST || 'smtp-relay.brevo.com',
      port: Number(process.env.SMTP_PORT || 587),
      secure: String(process.env.SMTP_SECURE || 'false') === 'true',
      auth: {
        user: smtpUser,
        pass: smtpPass
      }
    });

    await transporter.sendMail({
      from: `"Sassbyte Website" <${process.env.SMTP_FROM || recipient}>`,
      to: recipient,
      replyTo: email,
      subject: `New inquiry from ${name}`,
      text: buildEmailText({ name, email, company, message })
    });

    return res.json({ success: true, message: 'Your inquiry was sent successfully.' });
  } catch (error) {
    console.error('Email send failed:', error);
    return res.status(500).json({ error: 'Unable to send the email right now. Please try again later.' });
  }
});

app.listen(port, '127.0.0.1', () => {
  console.log(`Sassbyte contact API running on http://127.0.0.1:${port}`);
});
