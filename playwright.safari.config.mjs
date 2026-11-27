import config from './playwright.config.mjs';

export default {
    ...config,
    projects: [{name:'safari'}],
};
