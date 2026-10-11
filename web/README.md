# Amo browser client

React, TypeScript and Vite power the shared phone, tablet and desktop interface. Production assets are served by the relay from `web/dist`.

```bash
npm ci
npm run build
npm run test:reliability
npm run lint
```

For UI development, `npm run dev` proxies API and WebSocket requests to the local relay. Run the relay separately with the configuration described in the [project README](../README.md).

Use Playwright fixture suites for keyboard, queue, gesture, history and layout changes. Fixtures do not send model requests. Credentials, real conversation logs and screenshots from private sessions must remain outside the repository.
