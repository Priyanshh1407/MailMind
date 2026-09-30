import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { LazyMotion, MotionConfig } from 'motion/react'

const motionFeatures = () => import('./motionFeatures.js').then(module => module.default)
import './index.css'
import './phase6.css'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <LazyMotion features={motionFeatures} strict>
      <MotionConfig reducedMotion='user'>
        <App />
      </MotionConfig>
    </LazyMotion>
  </StrictMode>,
)
