import fs from 'node:fs'
import { importer } from 'ipfs-unixfs-importer'
import { MemoryBlockstore } from 'blockstore-core'
import { CID } from 'multiformats/cid'
import { base32 } from 'multiformats/bases/base32'
import { decode } from '@ipld/dag-pb'

const file = process.argv[2], chunkSize = Number(process.argv[3] || 4194304)
const buf = fs.readFileSync(file)
async function* fixed(source) {
  let acc = Buffer.alloc(0)
  const it = (source && typeof source[Symbol.asyncIterator] === 'function') ? source : [source]
  for await (const c of it) {
    acc = Buffer.concat([acc, Buffer.from(c)])
    while (acc.length >= chunkSize) { yield acc.subarray(0, chunkSize); acc = acc.subarray(chunkSize) }
  }
  if (acc.length) yield acc
}
const bs = new MemoryBlockstore()
let root
for await (const e of importer([{ content: new Uint8Array(buf) }], bs, {
  cidVersion: 1, rawLeaves: true, chunker: fixed, maxChunkSize: chunkSize,
})) root = e.cid.toString()

let nodes = 0, leaves = 0, links = 0
const walk = async (s) => {
  const cid = CID.parse(s, base32)
  const blk = await bs.get(cid)
  nodes++
  if (cid.code === 0x70) {
    const node = decode(blk)
    links = Math.max(links, node.Links.length)
    for (const l of node.Links) await walk(l.Hash.toString())
  } else leaves++
}
await walk(root)
console.log(JSON.stringify({ file, size: buf.length, chunkSize, root, dag_nodes: nodes, leaves, max_links: links }))
