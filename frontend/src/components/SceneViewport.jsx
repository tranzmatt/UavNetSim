import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { Grid, Line, OrbitControls } from '@react-three/drei'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { MTLLoader } from 'three/examples/jsm/loaders/MTLLoader.js'
import { OBJLoader } from 'three/examples/jsm/loaders/OBJLoader.js'
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js'

const MATERIAL_COLORS = {
  itu_concrete: '#5f6669',
  itu_brick: '#80584d',
  itu_glass: '#4f747c',
  itu_metal: '#8b9294',
  itu_wood: '#74644f',
  itu_medium_dry_ground: '#52604d',
  itu_wet_ground: '#315a68',
}

function SceneEnvironment() {
  const { gl, scene } = useThree()
  useEffect(() => {
    const room = new RoomEnvironment()
    const pmrem = new THREE.PMREMGenerator(gl)
    const environment = pmrem.fromScene(room, 0.04).texture
    const previousEnvironment = scene.environment
    const previousEnvironmentIntensity = scene.environmentIntensity
    const previousToneMapping = gl.toneMapping
    const previousExposure = gl.toneMappingExposure

    scene.environment = environment
    scene.environmentIntensity = 0.9
    gl.toneMapping = THREE.ACESFilmicToneMapping
    gl.toneMappingExposure = 1.08

    room.dispose()
    pmrem.dispose()
    return () => {
      scene.environment = previousEnvironment
      scene.environmentIntensity = previousEnvironmentIntensity
      gl.toneMapping = previousToneMapping
      gl.toneMappingExposure = previousExposure
      environment.dispose()
    }
  }, [gl, scene])
  return null
}

function configureOsm2WorldMaterial(material) {
  if (!material?.name?.toUpperCase().includes('GLASS') || !material.isMeshStandardMaterial) return

  // OSM2World's combined PBR map makes facade glass highly metallic and nearly
  // mirror-smooth. In the simulator's dark scene that reflects as solid black.
  material.metalnessMap = null
  material.roughnessMap = null
  material.metalness = 0.12
  material.roughness = 0.28
  material.envMapIntensity = 0.9
  material.needsUpdate = true
}

function terrainHeightAt(terrain, sizeX, sizeY, x, y) {
  if (!terrain) return 0
  const column = THREE.MathUtils.clamp((x / sizeX) * (terrain.columns - 1), 0, terrain.columns - 1)
  const row = THREE.MathUtils.clamp((y / sizeY) * (terrain.rows - 1), 0, terrain.rows - 1)
  const left = Math.min(terrain.columns - 2, Math.floor(column))
  const lower = Math.min(terrain.rows - 2, Math.floor(row))
  const fractionX = column - left
  const fractionY = row - lower
  const base = lower * terrain.columns + left
  const southwest = terrain.vertices[base].z
  const southeast = terrain.vertices[base + 1].z
  const northwest = terrain.vertices[base + terrain.columns].z
  const northeast = terrain.vertices[base + terrain.columns + 1].z
  const south = THREE.MathUtils.lerp(southwest, southeast, fractionX)
  const north = THREE.MathUtils.lerp(northwest, northeast, fractionX)
  return THREE.MathUtils.lerp(south, north, fractionY)
}

function CameraTarget({ scene }) {
  const controls = useRef()
  const { camera } = useThree()
  useEffect(() => {
    const scale = Math.max(scene.size_x, scene.size_y)
    const relief = Math.max(0, ...(scene.terrain?.vertices || []).map((point) => point.z))
    const structureHeight = Math.max(
      0,
      ...scene.features
        .filter((feature) => feature.category === 'building')
        .map((feature) => (feature.footprint[0]?.z || 0) + feature.height),
    )
    const viewHeight = Math.max(scale, relief + scale * 0.65, structureHeight * 1.8)
    camera.position.set(scene.size_x * 1.25, viewHeight, scene.size_y * 0.75)
    controls.current?.target.set(
      scene.size_x / 2,
      Math.max(relief * 0.28 + 12, structureHeight * 0.35),
      -scene.size_y / 2,
    )
    controls.current?.update()
  }, [camera, scene])
  return <OrbitControls ref={controls} makeDefault maxPolarAngle={Math.PI / 2.05} minDistance={40} maxDistance={1800} />
}

function Roof({ feature, baseHeight }) {
  const roofShape = feature.osm_tags?.['roof:shape'] || 'flat'
  const roofHeight = Number(feature.osm_tags?.['roof:height'] || 0)
  const geometry = useMemo(() => {
    if (roofShape === 'flat' || roofHeight <= 0.01) return null
    const shape = new THREE.Shape()
    feature.footprint.forEach((point, index) => {
      if (index === 0) shape.moveTo(point.x, point.y)
      else shape.lineTo(point.x, point.y)
    })
    shape.closePath()
    const result = new THREE.ExtrudeGeometry(shape, {
      depth: roofHeight,
      bevelEnabled: roofShape === 'hipped' || roofShape === 'pyramidal',
      bevelSegments: 1,
      bevelSize: Math.min(1.2, roofHeight * 0.18),
      bevelThickness: Math.min(1.2, roofHeight * 0.18),
    })
    result.rotateX(-Math.PI / 2)
    return result
  }, [feature, roofHeight, roofShape])
  if (!geometry) return null
  return (
    <mesh geometry={geometry} position={[0, baseHeight + feature.height, 0]} castShadow receiveShadow>
      <meshStandardMaterial
        color={feature.osm_tags?.['roof:colour'] || '#8c6956'}
        roughness={feature.osm_tags?.['roof:material'] === 'glass' ? 0.28 : 0.78}
        metalness={feature.osm_tags?.['roof:material'] === 'metal' ? 0.32 : 0.04}
      />
    </mesh>
  )
}

function Building({ feature, selected }) {
  const baseHeight = useMemo(
    () => feature.footprint.reduce((total, point) => total + (point.z || 0), 0) / feature.footprint.length,
    [feature],
  )
  const geometry = useMemo(() => {
    const shape = new THREE.Shape()
    feature.footprint.forEach((point, index) => {
      if (index === 0) shape.moveTo(point.x, point.y)
      else shape.lineTo(point.x, point.y)
    })
    shape.closePath()
    const result = new THREE.ExtrudeGeometry(shape, { depth: feature.height, bevelEnabled: false })
    result.rotateX(-Math.PI / 2)
    return result
  }, [feature])
  return (
    <group>
      <mesh geometry={geometry} position={[0, baseHeight, 0]} castShadow receiveShadow>
        <meshStandardMaterial
          color={feature.osm_tags?.['facade:colour'] || MATERIAL_COLORS[feature.material] || MATERIAL_COLORS.itu_concrete}
          roughness={selected ? 0.36 : 0.62}
          metalness={feature.material === 'itu_metal' ? 0.52 : 0.08}
        />
      </mesh>
      <Roof feature={feature} baseHeight={baseHeight} />
    </group>
  )
}

function osm2WorldOriginPosition(object, scene) {
  const origin = object.userData?.origin
  const latitude = Number(origin?.lat)
  const longitude = Number(origin?.lon)
  if (Number.isFinite(latitude) && Number.isFinite(longitude)) {
    const metresPerDegreeLongitude = Math.cos(THREE.MathUtils.degToRad(scene.anchor.latitude)) * 111320
    return {
      x: (longitude - scene.anchor.longitude) * metresPerDegreeLongitude,
      z: -(latitude - scene.anchor.latitude) * 110574,
    }
  }
  return { x: scene.size_x / 2, z: -scene.size_y / 2 }
}

function alignOsm2WorldModel(object, scene) {
  const origin = osm2WorldOriginPosition(object, scene)
  object.position.x = origin.x
  object.position.z = origin.z
  const bounds = new THREE.Box3().setFromObject(object)
  object.position.y -= bounds.min.y
}

function featureForOsm2WorldObject(object, scene, fallbackIndex, category) {
  const identifier = object.userData?.osmId || object.name || ''
  const match = String(identifier).match(/w(-?\d+)$/)
  if (match) {
    const featureIndex = -Number(match[1]) - 2000000000
    const feature = scene.features[featureIndex]
    if (feature?.category === category) return feature
  }
  return scene.features.filter((feature) => feature.category === category)[fallbackIndex] || null
}

function osm2WorldSurfaceKind(child) {
  const name = child.name.toUpperCase()
  const materials = Array.isArray(child.material) ? child.material : [child.material]
  const materialNames = materials.map((material) => material?.name?.toUpperCase() || '')
  if (name.startsWith('WATER') || materialNames.some((name) => name === 'WATER' || name.includes('WATER'))) return 'water'
  if (name.startsWith('ROAD') || materialNames.some((name) => name === 'ASPHALT')) return 'road'
  if (name.startsWith('SURFACEAREA')) return 'land'
  if (materialNames.some((name) => name === 'TERRAIN_DEFAULT' || name === 'GRASS')) return 'land'
  return null
}

function osm2WorldSurfaceLayers(object) {
  const layers = { road: false, water: false }
  object.traverse((child) => {
    const kind = osm2WorldSurfaceKind(child)
    if (kind === 'road' || kind === 'water') layers[kind] = true
  })
  return layers
}

function hasOsm2WorldSurfaceAncestor(object, kind) {
  let ancestor = object.parent
  while (ancestor) {
    if (osm2WorldSurfaceKind(ancestor) === kind) return true
    ancestor = ancestor.parent
  }
  return false
}

function drapeOsm2WorldSurface(root, scene, feature, kind) {
  if (!scene.terrain) return
  const bounds = new THREE.Box3().setFromObject(root)
  const baseHeight = bounds.min.y
  const lift = kind === 'water' ? 0.18 : feature?.osm_tags?.bridge === 'yes' ? 1.5 : 0.28
  const position = new THREE.Vector3()
  root.traverse((child) => {
    if (!child.isMesh || !child.geometry?.attributes?.position) return
    const positions = child.geometry.attributes.position
    for (let index = 0; index < positions.count; index += 1) {
      position.fromBufferAttribute(positions, index)
      child.localToWorld(position)
      const relativeHeight = position.y - baseHeight
      position.y = terrainHeightAt(scene.terrain, scene.size_x, scene.size_y, position.x, -position.z) + lift + relativeHeight
      child.worldToLocal(position)
      positions.setXYZ(index, position.x, position.y, position.z)
    }
    positions.needsUpdate = true
    child.geometry.computeVertexNormals()
    child.geometry.computeBoundingBox()
    child.geometry.computeBoundingSphere()
  })
}

function placeOsm2WorldBuildings(object, scene) {
  let fallbackIndex = 0
  const surfaceRoots = []
  object.updateMatrixWorld(true)
  object.traverse((child) => {
    const surfaceKind = osm2WorldSurfaceKind(child)
    if (surfaceKind === 'land') {
      child.visible = false
      return
    }
    if (surfaceKind === 'road' || surfaceKind === 'water') {
      if (hasOsm2WorldSurfaceAncestor(child, surfaceKind)) return
      const feature = featureForOsm2WorldObject(
        child,
        scene,
        surfaceRoots.filter((surface) => surface.kind === surfaceKind).length,
        surfaceKind,
      )
      surfaceRoots.push({ root: child, feature, kind: surfaceKind })
      return
    }
    if (!child.name.startsWith('Building')) return
    const feature = featureForOsm2WorldObject(child, scene, fallbackIndex, 'building')
    fallbackIndex += 1
    if (!feature) return
    const bounds = new THREE.Box3().setFromObject(child)
    const baseHeight = feature.footprint.reduce((total, point) => total + (point.z || 0), 0) / feature.footprint.length
    child.position.y += baseHeight - bounds.min.y
  })
  object.updateMatrixWorld(true)
  surfaceRoots.forEach(({ root, feature, kind }) => drapeOsm2WorldSurface(root, scene, feature, kind))
}

function Osm2WorldModel({ scene, onError, onReady }) {
  const model = useRef(null)
  const [object, setObject] = useState(null)
  const rendering = scene.rendering
  const fallbackAssetVersion = `${scene.anchor.latitude}:${scene.anchor.longitude}:${scene.size_x}:${scene.size_y}:${scene.features.length}`
  const assetVersion = rendering?.asset_version || fallbackAssetVersion
  const url = rendering?.model_file
    ? `/artifacts/scene/${rendering.model_file}?scene=${encodeURIComponent(assetVersion)}`
    : null
  useEffect(() => {
    if (!url || !rendering?.model_format) return undefined
    let disposed = false
    const finish = (asset) => {
      if (disposed) return
      const next = asset.scene || asset
      next.traverse((child) => {
        if (child.isMesh) {
          child.castShadow = true
          child.receiveShadow = true
          const materials = Array.isArray(child.material) ? child.material : [child.material]
          materials.forEach(configureOsm2WorldMaterial)
        }
      })
      alignOsm2WorldModel(next, scene)
      placeOsm2WorldBuildings(next, scene)
      model.current = next
      setObject(next)
      onReady(osm2WorldSurfaceLayers(next))
    }
    const fail = () => {
      if (!disposed) onError()
    }
    if (rendering.model_format !== 'obj') {
      new GLTFLoader().load(url, finish, undefined, fail)
    } else {
      const objLoader = new OBJLoader()
      const loadObj = () => objLoader.load(url, finish, undefined, fail)
      const materialUrl = url.replace(/\.obj(\?.*)?$/i, '.mtl$1')
      new MTLLoader().load(materialUrl, (materials) => {
        materials.preload()
        objLoader.setMaterials(materials)
        loadObj()
      }, undefined, loadObj)
    }
    return () => {
      disposed = true
      if (model.current) {
        model.current.traverse((child) => {
          child.geometry?.dispose()
          if (Array.isArray(child.material)) child.material.forEach((material) => material.dispose())
          else child.material?.dispose()
        })
      }
      model.current = null
      setObject(null)
    }
  }, [onError, onReady, rendering?.model_format, scene, url])
  if (!object) return null
  return <primitive object={object} />
}

function Surface({ feature, terrain, sizeX, sizeY }) {
  const geometry = useMemo(() => {
    const shape = new THREE.Shape()
    feature.footprint.forEach((point, index) => {
      if (index === 0) shape.moveTo(point.x, point.y)
      else shape.lineTo(point.x, point.y)
    })
    shape.closePath()
    const result = new THREE.ShapeGeometry(shape)
    result.rotateX(-Math.PI / 2)
    if (terrain) {
      const position = new THREE.Vector3()
      const positions = result.attributes.position
      for (let index = 0; index < positions.count; index += 1) {
        position.fromBufferAttribute(positions, index)
        positions.setY(index, terrainHeightAt(terrain, sizeX, sizeY, position.x, -position.z))
      }
      positions.needsUpdate = true
      result.computeVertexNormals()
      result.computeBoundingBox()
      result.computeBoundingSphere()
    }
    return result
  }, [feature, sizeX, sizeY, terrain])
  const baseHeight = terrain ? 0 : feature.footprint.reduce((total, point) => total + (point.z || 0), 0) / feature.footprint.length
  return (
    <mesh geometry={geometry} position={[0, baseHeight + (feature.category === 'water' ? 0.18 : 0.08), 0]} receiveShadow>
      <meshStandardMaterial
        color={MATERIAL_COLORS[feature.material] || MATERIAL_COLORS.itu_medium_dry_ground}
        roughness={feature.category === 'water' ? 0.28 : 0.96}
        metalness={feature.category === 'water' ? 0.08 : 0}
      />
    </mesh>
  )
}

function Terrain({ terrain }) {
  const geometry = useMemo(() => {
    const result = new THREE.BufferGeometry()
    const elevations = terrain.vertices.map((point) => point.z)
    const minimum = Math.min(...elevations)
    const range = Math.max(1, Math.max(...elevations) - minimum)
    const lowColor = new THREE.Color('#35413c')
    const highColor = new THREE.Color('#647069')
    result.setAttribute('position', new THREE.Float32BufferAttribute(
      terrain.vertices.flatMap((point) => [point.x, point.z, -point.y]), 3,
    ))
    result.setAttribute('color', new THREE.Float32BufferAttribute(
      terrain.vertices.flatMap((point) => {
        const color = lowColor.clone().lerp(highColor, (point.z - minimum) / range)
        return [color.r, color.g, color.b]
      }), 3,
    ))
    result.setIndex(terrain.faces.flat())
    result.computeVertexNormals()
    return result
  }, [terrain])
  return <mesh geometry={geometry} receiveShadow><meshBasicMaterial vertexColors side={THREE.DoubleSide} /></mesh>
}

function SceneBoundary({ scene }) {
  const points = useMemo(() => {
    if (!scene.terrain) {
      return [[0, 0.35, 0], [scene.size_x, 0.35, 0], [scene.size_x, 0.35, -scene.size_y], [0, 0.35, -scene.size_y], [0, 0.35, 0]]
    }
    const { rows, columns, vertices } = scene.terrain
    const perimeter = []
    for (let column = 0; column < columns; column += 1) perimeter.push(vertices[column])
    for (let row = 1; row < rows; row += 1) perimeter.push(vertices[row * columns + columns - 1])
    for (let column = columns - 2; column >= 0; column -= 1) perimeter.push(vertices[(rows - 1) * columns + column])
    for (let row = rows - 2; row > 0; row -= 1) perimeter.push(vertices[row * columns])
    perimeter.push(vertices[0])
    return perimeter.map((point) => [point.x, point.z + 0.8, -point.y])
  }, [scene])
  return <Line points={points} color="#f3a63a" lineWidth={2.2} transparent opacity={0.92} />
}

function Drone({ node, selected, onSelect }) {
  const group = useRef()
  useFrame((state) => {
    if (group.current) group.current.rotation.y = state.clock.elapsedTime * 0.45 + node.id
  })
  const position = [node.position[0], node.position[2], -node.position[1]]
  return (
    <group ref={group} position={position} onClick={(event) => { event.stopPropagation(); onSelect(node.id) }}>
      <mesh castShadow><sphereGeometry args={[selected ? 3.8 : 3.2, 16, 12]} /><meshStandardMaterial color={selected ? '#f3a63a' : '#d9dee0'} emissive={selected ? '#6a3b08' : '#172124'} /></mesh>
      <mesh rotation={[0, 0, Math.PI / 2]}><cylinderGeometry args={[0.55, 0.55, 15, 8]} /><meshStandardMaterial color="#6f797c" /></mesh>
      <mesh rotation={[Math.PI / 2, 0, 0]}><cylinderGeometry args={[0.55, 0.55, 15, 8]} /><meshStandardMaterial color="#6f797c" /></mesh>
      {[[7, 0], [-7, 0], [0, 7], [0, -7]].map(([x, z], index) => <mesh key={index} position={[x, 0, z]} rotation={[0, index * 0.3, 0]}><cylinderGeometry args={[3.2, 3.2, 0.28, 20]} /><meshStandardMaterial color="#f3a63a" transparent opacity={0.62} /></mesh>)}
      <mesh position={[0, -node.position[2] / 2, 0]}><cylinderGeometry args={[0.12, 0.12, node.position[2], 6]} /><meshBasicMaterial color="#89a7ad" transparent opacity={selected ? 0.32 : 0.12} /></mesh>
    </group>
  )
}

function PacketArc({ arc, nodes }) {
  const pulse = useRef()
  const source = nodes.find((node) => node.id === arc.source)
  const destination = nodes.find((node) => node.id === arc.destination)
  const curve = useMemo(() => {
    if (!source || !destination) return null
    const start = new THREE.Vector3(source.position[0], source.position[2], -source.position[1])
    const end = new THREE.Vector3(destination.position[0], destination.position[2], -destination.position[1])
    const lift = Math.max(15, start.distanceTo(end) * 0.12)
    const middle = start.clone().lerp(end, 0.5).add(new THREE.Vector3(0, lift, 0))
    return new THREE.QuadraticBezierCurve3(start, middle, end)
  }, [destination, source])
  useFrame(() => {
    if (!pulse.current || !curve) return
    pulse.current.position.copy(curve.getPoint(Math.min(1, (Date.now() - arc.createdAt) / 900)))
  })
  if (!curve) return null
  const color = arc.status === 'failed' ? '#ef5c50' : arc.status === 'success' ? '#46c8b0' : '#f3a63a'
  return <group><Line points={curve.getPoints(30)} color={color} lineWidth={1.4} transparent opacity={0.7} /><mesh ref={pulse}><sphereGeometry args={[1.8, 10, 8]} /><meshBasicMaterial color={color} /></mesh></group>
}

export default function SceneViewport({ scene, nodes, arcs, selectedNode, onSelectNode }) {
  const [modelFailed, setModelFailed] = useState(false)
  const [modelLoaded, setModelLoaded] = useState(false)
  const [modelLayers, setModelLayers] = useState({ road: false, water: false })
  const hasOsm2WorldModel = scene.rendering?.status === 'rendered' && Boolean(scene.rendering.model_file)
  useEffect(() => {
    setModelFailed(false)
    setModelLoaded(false)
    setModelLayers({ road: false, water: false })
  }, [scene])
  const handleModelError = useCallback(() => {
    setModelLayers({ road: false, water: false })
    setModelFailed(true)
  }, [])
  const handleModelReady = useCallback((layers) => {
    setModelLayers(layers)
    setModelLoaded(true)
  }, [])
  const detailedModelActive = hasOsm2WorldModel && !modelFailed && modelLoaded
  return (
    <Canvas shadows dpr={[1, 1.7]} camera={{ fov: 46, near: 0.5, far: 5000 }}>
      <color attach="background" args={['#171a1c']} /><fog attach="fog" args={['#171a1c', 650, 1600]} />
      <SceneEnvironment />
      <ambientLight intensity={0.58} /><directionalLight position={[250, 500, 180]} intensity={1.9} castShadow shadow-mapSize={[2048, 2048]} />
      {scene.terrain ? <Terrain terrain={scene.terrain} /> : <mesh position={[scene.size_x / 2, -0.2, -scene.size_y / 2]} rotation={[-Math.PI / 2, 0, 0]} receiveShadow><planeGeometry args={[scene.size_x + 100, scene.size_y + 100]} /><meshStandardMaterial color="#24282a" roughness={0.92} /></mesh>}
      {!scene.terrain && <Grid position={[scene.size_x / 2, 0, -scene.size_y / 2]} args={[scene.size_x, scene.size_y]} cellSize={20} cellThickness={0.45} cellColor="#42494b" sectionSize={100} sectionThickness={0.8} sectionColor="#5d6668" fadeDistance={900} />}
      <SceneBoundary scene={scene} />
      {hasOsm2WorldModel && !modelFailed && <Osm2WorldModel scene={scene} onError={handleModelError} onReady={handleModelReady} />}
      {(!hasOsm2WorldModel || modelFailed || !modelLoaded) && scene.features.filter((feature) => feature.category === 'building').map((feature) => <Building key={feature.id} feature={feature} />)}
      {scene.features.filter((feature) => ((feature.category === 'water' && (!detailedModelActive || !modelLayers.water)) || (!scene.terrain && feature.category === 'terrain'))).map((feature) => <Surface key={feature.id} feature={feature} terrain={scene.terrain} sizeX={scene.size_x} sizeY={scene.size_y} />)}
      {scene.features.filter((feature) => feature.category === 'road' && (!detailedModelActive || !modelLayers.road)).map((feature) => <Line key={feature.id} points={feature.footprint.map((point) => [point.x, (scene.terrain ? terrainHeightAt(scene.terrain, scene.size_x, scene.size_y, point.x, point.y) : point.z || 0) + 0.22, -point.y])} color="#697173" lineWidth={3} />)}
      {nodes.map((node) => <Drone key={node.id} node={node} selected={selectedNode === node.id} onSelect={onSelectNode} />)}
      {arcs.map((arc) => <PacketArc key={`${arc.id}-${arc.destination}`} arc={arc} nodes={nodes} />)}
      <CameraTarget scene={scene} />
    </Canvas>
  )
}
