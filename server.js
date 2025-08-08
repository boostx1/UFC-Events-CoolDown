const express = require('express');
const axios = require('axios');
const cheerio = require('cheerio');
const cors = require('cors');

const app = express();
app.use(cors());

app.get('/next-event', async (req, res) => {
  try {
    const response = await axios.get('https://www.ufc.com/events');
    const $ = cheerio.load(response.data);
    const event = $('.view-events .views-row').first();
    const name = event.find('.card-title').text().trim();
    const datetime = event.find('time').attr('datetime');
    res.json({ name, datetime });
  } catch (err) {
    res.status(500).json({ error: 'Failed to fetch event' });
  }
});

app.listen(3000, () => console.log('Server running on http://localhost:3000'));
