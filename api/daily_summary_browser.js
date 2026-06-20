const chromium = require('@sparticuz/chromium');
const puppeteer = require('puppeteer-core');

const SEARCH_URL =
  'https://www.marchespublics.gov.ma/index.php?page=entreprise.EntrepriseAdvancedSearch&searchAnnCons';
const BASE_URL = 'https://www.marchespublics.gov.ma';

function authorized(req) {
  const expected = (process.env.CRON_SECRET || '').trim();
  if (!expected) return false;
  const provided = String(req.query.secret || '').trim();
  return provided === expected;
}

function parseDate(req) {
  const value = String(req.query.date || '').trim();
  if (!value) {
    throw new Error('missing_date');
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error('invalid_date');
  }
  return new Date(`${value}T00:00:00Z`);
}

function formatFrDate(date) {
  const day = String(date.getUTCDate()).padStart(2, '0');
  const month = String(date.getUTCMonth() + 1).padStart(2, '0');
  const year = String(date.getUTCFullYear());
  return `${day}/${month}/${year}`;
}

function nextDay(date) {
  return new Date(date.getTime() + 24 * 60 * 60 * 1000);
}

module.exports = async (req, res) => {
  if (!authorized(req)) {
    return res.status(401).json({ ok: false, error: 'unauthorized' });
  }

  let browser;
  try {
    const targetDate = parseDate(req);
    const startDate = formatFrDate(targetDate);
    const endDate = formatFrDate(nextDay(targetDate));

    browser = await puppeteer.launch({
      args: chromium.args,
      defaultViewport: chromium.defaultViewport || { width: 1440, height: 2200 },
      executablePath: await chromium.executablePath(),
      headless: chromium.headless,
    });

    const page = await browser.newPage();
    await page.goto(SEARCH_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });

    await page.select('[name="ctl0$CONTENU_PAGE$AdvancedSearch$procedureType"]', '50');
    await page.$eval(
      '[name="ctl0$CONTENU_PAGE$AdvancedSearch$dateMiseEnLigneCalculeStart"]',
      (el, value) => {
        el.value = value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
      },
      startDate,
    );
    await page.$eval(
      '[name="ctl0$CONTENU_PAGE$AdvancedSearch$dateMiseEnLigneCalculeEnd"]',
      (el, value) => {
        el.value = value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
      },
      endDate,
    );

    await Promise.all([
      page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 60000 }),
      page.click('[name="ctl0$CONTENU_PAGE$AdvancedSearch$lancerRecherche"]'),
    ]);

    if (await page.$('[name="ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop"]')) {
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 60000 }),
        page.select('[name="ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop"]', '500'),
      ]);
    }

    const items = await page.$$eval('table.table-results tr', (rows, baseUrl) => {
      const pickDate = (text) => {
        const matches = String(text).match(/\d{2}\/\d{2}\/\d{4}/g);
        return matches && matches.length ? matches[matches.length - 1] : '';
      };
      const clean = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
      const extractTitle = (detailText) => {
        const text = clean(detailText);
        const marker = text.toLowerCase().indexOf('objet :');
        if (marker === -1) return text;
        return clean(text.slice(marker + 'objet :'.length));
      };
      const extractCategory = (metaParts) => {
        const value = clean(metaParts[1] || '');
        return value.replace(/\d{2}\/\d{2}\/\d{4}.*/, '').trim() || '—';
      };
      const extractLocation = (value) => {
        return clean(value).replace(/^\-\s*/, '') || '—';
      };

      return rows.slice(2).map((row) => {
        const cells = Array.from(row.querySelectorAll('td'));
        if (cells.length < 5) return null;

        const metaText = clean(cells[1]?.innerText || '');
        const metaParts = metaText.split('...').map((part) => clean(part)).filter(Boolean);
        const detailText = clean(cells[2]?.innerText || '');
        const locationText = clean(cells[3]?.innerText || '');
        const dueText = clean(cells[4]?.innerText || '');
        const link = row.querySelector('a[href*="EntrepriseDetailConsultation"]');
        const href = link ? new URL(link.getAttribute('href'), baseUrl).toString() : '';
        const title = extractTitle(detailText);
        const reference = (() => {
          try {
            return new URL(href).searchParams.get('refConsultation') || '';
          } catch {
            return '';
          }
        })();
        const dueMatch = dueText.match(/\d{2}\/\d{2}\/\d{4}(?:\s+\d{2}:\d{2})?/);
        return {
          reference,
          title,
          category: extractCategory(metaParts),
          location: extractLocation(locationText),
          due_date: dueMatch ? dueMatch[0] : '—',
          published_date: pickDate(metaText),
          consultation_url: href,
          procedure: clean(metaText.split('...')[0] || ''),
        };
      }).filter(Boolean);
    }, BASE_URL);

    const filtered = items.filter(
      (item) => item.published_date === startDate && item.procedure === 'AOS' && item.consultation_url,
    );

    return res.status(200).json({
      ok: true,
      date: targetDate.toISOString().slice(0, 10),
      items: filtered,
    });
  } catch (error) {
    return res.status(500).json({
      ok: false,
      error: String(error && error.message ? error.message : error),
    });
  } finally {
    if (browser) {
      await browser.close().catch(() => {});
    }
  }
};
