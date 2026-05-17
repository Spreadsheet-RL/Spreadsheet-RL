import React from 'react';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';

export default function ApiPage(): JSX.Element {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title={`API Documentation`}
      description="API Documentation for Sandbox Fusion">
      <main>
        hilo
      </main>
    </Layout>
  );
}