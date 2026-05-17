import clsx from 'clsx';
import Heading from '@theme/Heading';
import styles from './styles.module.css';

type FeatureItem = {
  title: string;
  image: string;
  description: JSX.Element;
};

const FeatureList: FeatureItem[] = [
  {
    title: 'Multi-language',
    image: require('@site/static/img/sandbox_languages.png').default,
    description: (
      <>
        Sandbox Fusion accommodates up to 20 programming languages, enabling comprehensive testing across diverse domains including coding, mathematics, and hardware programming.
      </>
    ),
  },
  {
    title: 'Multi-dataset',
    image: require('@site/static/img/datasets.png').default,
    description: (
      <>
        The Sandbox incorporates over 10 coding-related evaluation datasets, featuring a standardized data format and accessible via a uniform HTTP API.
      </>
    ),
  },
  {
    title: 'Production ready',
    image: require('@site/static/img/shield.png').default,
    description: (
      <>
        Sandbox Fusion is optimized for cloud infrastructure deployment and offers built-in security isolation when privileged container is available.
      </>
    ),
  },
];

function Feature({title, image, description}: FeatureItem) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center">
        <img className={styles.featureImg} src={image} alt={title} />
      </div>
      <div className="text--center padding-horiz--md">
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures(): JSX.Element {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}