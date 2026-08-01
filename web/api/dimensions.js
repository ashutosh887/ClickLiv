import { query, send } from './_clickhouse.js';

const PLATFORMS = ['ANDROID_PHONE', 'ANDROID_TAB', 'FIRE_TV', 'IPHONE', 'JIO_ANDROID_TV',
                   'LG_HTML_TV', 'Mweb', 'SAMSUNG_HTML_TV', 'SONY_ANDROID_TV',
                   'XIAOMI_ANDROID_TV'];
const VIDEO_TYPES = ['live', 'vod'];

const PEAK = `
SELECT max(peak_concurrency)
FROM marts.v_concurrency(
    grain_minutes = 1440, country = '', platform = {platform:String},
    video_type = {video_type:String}, content_id = 0,
    minute_from = 0, minute_to = 4294967295)`;

async function peakFor(platform, videoType) {
  const result = await query(PEAK, { platform, video_type: videoType });
  const value = result.data?.[0]?.[0];
  return value === null || value === undefined ? 0 : Number(value);
}

export default async function handler(req, res) {
  try {
    const platforms = await Promise.all(
      PLATFORMS.map(async (name) => ({ name, peak: await peakFor(name, '') })));
    const videoTypes = await Promise.all(
      VIDEO_TYPES.map(async (name) => ({ name, peak: await peakFor('', name) })));
    return send(res, 200, {
      overall_peak: await peakFor('', ''),
      platforms: platforms.filter((row) => row.peak > 0).sort((a, b) => b.peak - a.peak),
      video_types: videoTypes.filter((row) => row.peak > 0).sort((a, b) => b.peak - a.peak),
    }, 3600);
  } catch (error) {
    return send(res, 502, { error: String(error.message || error) }, 0);
  }
}
